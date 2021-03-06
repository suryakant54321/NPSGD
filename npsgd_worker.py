#!/usr/bin/python
# Author: Thomas Dimson [tdimson@gmail.com]
# Date:   January 2011
# For distribution details, see LICENSE
"""Worker process for NPSGD.

This script will periodically poll the queue server specified in the config
file to see if it has any jobs available. When it finds one, it will 
take it off the queue and begin processing - and communicate success back
with the user (via e-mail) or failure back to the queue
"""
import os
import sys
import time
import json
import logging
import urllib2, urllib
from threading import Thread, Event
from optparse import OptionParser

from npsgd import model_manager
from npsgd.config import config
from npsgd.model_task import ModelTask
from npsgd.model_manager import modelManager
import npsgd.email_manager

class TaskKeepAliveThread(Thread):
    """Model keep alive thread.

    Periodically sends a keepalive (heartbeat) to the server while we are working
    on a model task so that it doesn't expire our task id. 
    """

    def __init__(self, keepAliveRequest, taskId):
        Thread.__init__(self)
        self.done             = Event()
        self.keepAliveRequest = keepAliveRequest
        self.taskId           = taskId
        self.daemon           = True

    def run(self):
        fails = 0
        while True:
            self.done.wait(config.keepAliveInterval)
            if self.done.isSet():
                break

            try:
                logging.info("Making heartbeat request '%s'", self.keepAliveRequest)
                response = urllib2.urlopen("%s/%s?secret=%s" % (self.keepAliveRequest, self.taskId, config.requestSecret))
            except urllib2.URLError, e:
                logging.error("Heartbeat failed to make connection to %s", self.keepAliveRequest)
                fails += 1


class NPSGDWorker(object):
    """Worker class for executing models and sending out result emails.

    This enters a polling loop where the worker will poll the queue for tasks
    at a fixed interval. When it finds a task, it will decode it into a model, 
    then process it using the model's "run" method.
    """
    def __init__(self, serverAddress, serverPort):
        self.baseRequest          = "http://%s:%s" % (serverAddress, serverPort)
        self.infoRequest          = "%s/worker_info"      % self.baseRequest
        self.taskRequest          = "%s/worker_work_task" % self.baseRequest
        self.failedTaskRequest    = "%s/worker_failed_task" % self.baseRequest
        self.hasTaskRequest       = "%s/worker_has_task" % self.baseRequest
        self.succeedTaskRequest    = "%s/worker_succeed_task" % self.baseRequest
        self.taskKeepAliveRequest = "%s/worker_keep_alive_task" % self.baseRequest
        self.requestTimeout  = 100
        self.supportedModels = ["test"]
        self.requestErrors   = 0
        self.maxErrors       = 3 
        self.errorSleepTime    = 10
        self.requestSleepTime = 10

    def getServerInfo(self):
        try:
            response = urllib2.urlopen("%s?secret=%s" % (self.infoRequest, config.requestSecret))
        except urllib2.URLError, e:
            logging.error("Failed to make initial connection to %s", self.baseRequest)
            return
        
        logging.info("Got initial response from server")

    def loop(self):
        """Main IO loop."""
        logging.info("Entering event loop")
        while True:
            try:
                self.handleEvents()
            except Exception:
                logging.exception("Unhandled exception in event loop!")
                
    def handleEvents(self):
        """Workhorse method of actually making requests to the queue for tasks."""
        try:
            logging.info("Polling %s for tasks" % self.taskRequest)
            #response = urllib2.urlopen("%s?secret=%s" % (self.taskRequest, config.requestSecret))
            response = urllib2.urlopen(self.taskRequest, data=urllib.urlencode({
                "secret": config.requestSecret,
                "model_versions_json": json.dumps(modelManager.modelVersions())
            }))
        except urllib2.URLError, e:
            self.requestErrors += 1
            logging.error("Error making worker request to server, attempt #%d", self.requestErrors + 1)
            time.sleep(self.errorSleepTime)
            return

        try:
            decodedResponse = json.load(response)
        except ValueError, e:
            self.requestErrors += 1
            logging.error("Error decoding server response, attempt #%d", self.requestErrors +1)
            time.sleep(self.errorSleepTime)
            return

        self.requestErrors = 0 
        self.processResponse(decodedResponse)
        time.sleep(self.requestSleepTime)



    def processResponse(self, response):
        if "status" in response:
            if response["status"] == "empty_queue":
                logging.info("No tasks available on server")
            elif response["status"] == "no_version":
                logging.info("Queue lacks any tasks with our model versions")
        elif "task" in response:
            self.processTask(response["task"])

    def notifyFailedTask(self, taskId):
        try:
            logging.info("Notifying server of failed task with id %s", taskId)
            response = urllib2.urlopen("%s/%s?secret=%s" % (self.failedTaskRequest, taskId, config.requestSecret))
        except urllib2.URLError, e:
            logging.error("Failed to communicate failed task to server %s", self.baseRequest)

    def notifySucceedTask(self, taskId):
        try:
            logging.info("Notifying server of succeeded task with id %s", taskId)
            response = urllib2.urlopen("%s/%s?secret=%s" % (self.succeedTaskRequest, taskId, config.requestSecret))
        except urllib2.URLError, e:
            logging.error("Failed to communicate succeeded task to server %s", self.baseRequest)

    def serverHasTask(self, taskId):
        """Method for ensuring that the queue still recognizes our task id.

        If the queue has expired the task for some reason (i.e. a timeout)
        this method will return false. Otherwise, it means we can proceed.
        """
        try:
            logging.info("Making has task request for %s", taskId)
            response = urllib2.urlopen("%s/%s?secret=%s" % (self.hasTaskRequest, taskId, config.requestSecret))
        except urllib2.URLError, e:
            logging.error("Failed to make has task request to server %s", self.baseRequest)
            raise RuntimeError(e)

        try:
            decodedResponse = json.load(response)
        except ValueError, e:
            logging.error("Bad response from server for has task")
            raise RuntimeError(e)
        
        if "response" in decodedResponse and decodedResponse["response"] in ["yes", "no"]:
            return decodedResponse["response"] == "yes"
        else:
            logging.error("Malformed response from server")
            raise RuntimeError("Malformed response from server for 'has task'")
        
    def processTask(self, taskDict):
        """Handle creation and running of a model and setup heartbeat thread.

        This is the heart of a worker. When we find a model on the queue, this
        method takes the request and decodes it into something that can be processed.
        It will then spawn a heartbeat thread that continues to check into the server
        while we actually enter the models "run" method. From there, it is all up
        to the model to handle.
        """
        taskId = None
        if "taskId" in taskDict:
            taskId = taskDict["taskId"]

        try:
            try:
                model = modelManager.getModel(taskDict["modelName"], taskDict["modelVersion"])
                logging.info("Creating a model task for '%s'", taskDict["modelName"])
                taskObject = model.fromDict(taskDict)
            except KeyError, e:
                logging.warning("Was unable to deserialize model task (%s), model task: %s", e, taskDict)
                if taskId:
                    self.notifyFailedTask(taskId)
                return

            keepAliveThread = TaskKeepAliveThread(self.taskKeepAliveRequest, taskObject.taskId)
            keepAliveThread.start()
            try:
                resultsEmail = taskObject.run()
                logging.info("Model finished running, sending email")
                if self.serverHasTask(taskObject.taskId):
                    npsgd.email_manager.blockingEmailSend(resultsEmail)
                    logging.info("Email sent, model is 100% complete!")
                    self.notifySucceedTask(taskObject.taskId)
                else:
                    logging.warning("Skipping task completion since the server forgot about our task")

            except RuntimeError, e:
                logging.error("Some kind of error during processing model task, notifying server of failure")
                logging.exception(e)
                self.notifyFailedTask(taskObject.taskId)

            finally:
                keepAliveThread.done.set()

        except: #If all else fails, notify the server that we are going down
            if taskId:
                self.notifyFailedTask(taskId)
            raise

def main():
    parser = OptionParser()
    parser.add_option('-c', '--config', dest="config",
            help="Configuration file path", type="string", default="config.cfg")

    parser.add_option('-l', '--log-filename', dest='log',
                        help="Log filename (use '-' for stderr)", default="-")

    (options, args) = parser.parse_args()

    config.loadConfig(options.config)
    config.setupLogging(options.log)
    model_manager.setupModels()
    model_manager.startScannerThread()

    worker = NPSGDWorker(config.queueServerAddress, config.queueServerPort)
    logging.info("NPSGD Worker booted up, going into event loop")
    worker.getServerInfo()
    worker.loop()

if __name__ == "__main__":
    main()
