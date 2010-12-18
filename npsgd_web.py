#!/usr/bin/python
import os
import sys
import time
import logging
import functools
import threading
import tornado.web
import tornado.ioloop
import tornado.escape
import tornado.httpclient
import tornado.httpserver
import urllib
from optparse import OptionParser
from datetime import datetime

from npsgd import model_manager
from npsgd import model_parameters
from npsgd import ui_modules

from npsgd.model_manager import modelManager
from npsgd.model_task import ModelTask
from npsgd.model_parameters import ValidationError
from npsgd.config import config

#This is a simple cache so we don't overload the queue with unnecessary requests
#just to see it has workers
lastWorkerCheckSuccess = datetime(1,1,1)

class ClientModelRequest(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self, modelName):
        global lastWorkerCheckSuccess
        model = modelManager.getLatestModel(modelName)

        td = datetime.now() - lastWorkerCheckSuccess
        checkWorkers = (td.seconds + td.days * 24 * 3600) > config.keepAliveTimeout

        if checkWorkers:
            #check with queue to see if we have workers
            http = tornado.httpclient.AsyncHTTPClient()
            request = tornado.httpclient.HTTPRequest(
                    "http://%s:%s/client_queue_has_workers" % (config.queueServerAddress, config.queueServerPort),
                    method="GET")
            callback = functools.partial(self.queueCallback, model=model)
            http.fetch(request, callback)
        else:
            self.renderModel(model)

    def renderModel(self, model):
        self.render(config.modelTemplatePath, model=model, errorText=None)

    def queueErrorRender(self, errorText):
        self.render(config.modelErrorTemplatePath, errorText=errorText)

    def queueCallback(self, response, model=None):
        global lastWorkerCheckSuccess
        if response.error: 
            self.queueErrorRender("We are sorry. Our queuing server appears to be down at the moment, please try again later")
            return

        try:
            json = tornado.escape.json_decode(response.body)
            hasWorkers = json["response"]["has_workers"]
            if hasWorkers:
                lastWorkerCheckSuccess = datetime.now()
                self.renderModel(model)
            else:
                self.queueErrorRender("We are sorry, our model worker machines appear to be down at the moment. Please try again later")
                return

        except KeyError:
            logging.info("Bad response from queue server")
            self.queueErrorRender("We are sorry. Our queuing server appears to be having issues communicating at the moment, please try again later")
            return

    @tornado.web.asynchronous
    def post(self, modelName):
        modelVersion = self.get_argument("modelVersion")
        model = modelManager.getModel(modelName, modelVersion)
        try:
            task = self.setupModelTask(model)
        except ValidationError, e:
            logging.exception(e)
            self.render(config.modelTemplatePath, model=model, errorText=str(e))
            return
        except tornado.web.HTTPError, e:
            logging.exception(e)
            self.render(config.modelTemplatePath, model=model, errorText=None)
            return

        logging.info("Making async request to get confirmation number for task")

        http = tornado.httpclient.AsyncHTTPClient()
        request = tornado.httpclient.HTTPRequest(
                "http://%s:%s/client_model_create" % (config.queueServerAddress, config.queueServerPort),
                method="POST",
                body=urllib.urlencode({"task_json": tornado.escape.json_encode(task.asDict())}))

        http.fetch(request, self.confirmationNumberCallback)

    def confirmationNumberCallback(self, response):
        if response.error: raise tornado.web.HTTPError(500)

        try:
            json = tornado.escape.json_decode(response.body)
            logging.info(json)
            res = json["response"]
            model = modelManager.getModel(res["task"]["modelName"], res["task"]["modelVersion"])
            task = model.fromDict(res["task"])
            code = res["code"]
        except KeyError:
            logging.info("Bad response from queue server")
            raise tornado.web.HTTPError(500)

        self.render(config.confirmTemplatePath, email=task.emailAddress, code=code)

    def setupModelTask(self, model):
        email = self.get_argument("email")

        paramDict = {}
        for param in model.parameters:
            try:
                argVal = self.get_argument(param.name)
            except tornado.web.HTTPError:
                argVal = param.nonExistValue()

            value = param.withValue(argVal)
            paramDict[param.name] = value.asDict()

        task = model(email, 0, paramDict)
        return task


class ClientBaseRequest(tornado.web.RequestHandler):
    def get(self):
        self.write("Find a model!")

class ClientConfirmRequest(tornado.web.RequestHandler):
    @tornado.web.asynchronous
    def get(self, confirmationCode):
        http = tornado.httpclient.AsyncHTTPClient()
        request = tornado.httpclient.HTTPRequest(
                "http://%s:%s/client_confirm/%s" % (config.queueServerAddress, config.queueServerPort, confirmationCode))

        logging.info("Asyncing a confirmation: '%s'", confirmationCode)
        http.fetch(request, self.confirmationCallback)

    def confirmationCallback(self, response):
        if response.error: raise tornado.web.HTTPError(404)

        json = tornado.escape.json_decode(response.body)
        res = json["response"]

        if res == "okay":
            self.render(config.confirmedTemplatePath)
        else:
            logging.info("Bad response from queue server: %s", res)
            raise tornado.web.HTTPError(500)
            

def setupClientApplication():
    appList = [ 
        (r"/", ClientBaseRequest),
        (r"/confirm_submission/(\w+)", ClientConfirmRequest),
        (r"/models/(.*)", ClientModelRequest)
    ]

    settings = {
        "static_path": os.path.join(os.path.dirname(__file__), "static"),
        "ui_modules": ui_modules,
        "template_path": config.htmlTemplateDirectory
    }

    return tornado.web.Application(appList, **settings)


def main():
    parser = OptionParser()
    parser.add_option('-c', '--config', dest='config',
                        help="Config file", default="config.cfg")
    parser.add_option('-p', '--client-port', dest='port',
                        help="Http port (for serving html)", default=8000, type="int")
    parser.add_option('-l', '--log-filename', dest='log',
                        help="Log filename (use '-' for stderr)", default="-")

    (options, args) = parser.parse_args()

    config.loadConfig(options.config)
    config.setupLogging(options.log)
    model_manager.setupModels()
    model_manager.startScannerThread()

    clientHTTP = tornado.httpserver.HTTPServer(setupClientApplication())
    clientHTTP.listen(options.port)

    logging.info("NPSGD Web Booted up, serving on port %d", options.port)
    print >>sys.stderr, "NPSGD web server running on port %d" % options.port

    tornado.ioloop.IOLoop.instance().start()

if __name__ == "__main__":
    main()
