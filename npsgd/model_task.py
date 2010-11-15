import os
import sys
import uuid
import logging
import subprocess
import email_manager

from model_manager import ModelMount

class LatexError(RuntimeError): pass
class ModelTask(object):
    __metaclass__ = ModelMount
    abstractModel = "ModelTask"

    #Every model short implement a subset of these
    short_name  = "unspecified_name"
    subtitle    = "Unspecified Subtitle"
    attachments = []

    def __init__(self, emailAddress, modelParameters={}):
        self.emailAddress      = emailAddress
        self.modelParameters   = []
        self.latexPreamblePath = "preamble.tex"
        self.latexFooterPath   = "footer.tex"
        self.workingDirectory  = "/var/tmp/npsgd/%s" % str(uuid.uuid4())

        for k,v in modelParameters.iteritems():
            param = self.parameterType(k).fromDict(v)
            setattr(self, param.name, param)
            self.modelParameters.append(param)

    def createWorkingDirectory(self):
        try:
            os.mkdir(self.workingDirectory)
        except OSError, e:
            logging.warning(e)

    def parameterType(self, parameterName):
        for pClass in self.__class__.parameters:
            if parameterName == pClass.name:
                return pClass

        return None


    @classmethod
    def fromDict(cls, dictionary):
        emailAddress = dictionary["emailAddress"]
        return cls(emailAddress, dictionary["modelParameters"])

    def asDict(self):
        return {
            "emailAddress" :   self.emailAddress,
            "modelName": self.__class__.short_name,
            "modelParameters": dict((p.name, p.asDict()) for p in self.modelParameters)
        }

    def latexBody(self):
        return "This is a test for %s" % self.emailAddress

    def latexParameterTable(self):
        paramRows = "\\\\\n".join(p.asLatexRow() for p in self.modelParameters)
        return """
        \\begin{centering}
        \\begin{tabular*}{6in}{@{\\extracolsep{\\fill}} c c c}
        \\textbf{Name} & \\textbf{Description} & \\textbf{Value} \\\\
        \\hline
        %s
        \\end{tabular*}
        \\end{centering}""" % paramRows

    def emailBody(self):
        return "Model run results from NPSG"

    def emailTitle(self):
        return "Model run results from NPSG"

    def getAttachments(self):
        pdf = self.generatePDF()

        attach = [('results.pdf', pdf)]
        for attachment in self.__class__.attachments:
            with open(os.path.join(self.workingDirectory, attachment)) as f:
                attach.append((attachment, f.read()))

        return attach

    def generatePDF(self):
        with open(self.latexPreamblePath) as pf:
            preamble = pf.read()
        with open(self.latexFooterPath) as ff:
            footer = ff.read()

        latex = "%s\n%s\n%s" % (preamble, self.latexBody(), footer)
        logging.info(latex)

        texPath = os.path.join(self.workingDirectory, "test_task.tex")
        pdfOutputPath = os.path.join(self.workingDirectory, "test_task.pdf")

        with open(texPath, 'w') as f:
            f.write(latex)

        logging.info("Calling PDFLatex to generate pdf output")
        retCode = subprocess.call(["pdflatex", "-halt-on-error", texPath], cwd=self.workingDirectory)
        logging.info("PDFLatex terminated with error code %d", retCode)

        if retCode != 0:
            raise LatexError("Bad exit code from latex")

        with open(pdfOutputPath, 'rb') as f:
            pdf = f.read()

        return pdf

    def sendResultsEmail(self, attachments=[]):
        logging.info("Sending results email")
        email_manager.sendMessage(self.emailAddress, "NPSG Model Run Results", """
Hi,

This email address recently requested a model run for the NPSG group at the university of
Waterloo. We are happy to report that the run succeeded. We have attached a pdf copy
of the results to this message.

Natural Phenomenon Simulation Group
University of Waterloo
""", attachments)
        logging.info("Sent!")

    def runModel(self):
        logging.warning("Called default run model - this should be overridden")

    def run(self):
        logging.info("Running default task for '%s'", self.emailAddress)
        self.createWorkingDirectory()
        self.runModel()
        self.sendResultsEmail(self.getAttachments())