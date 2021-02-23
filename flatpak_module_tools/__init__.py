import logging


class MBSWarningFilter(logging.Filter):
    def filter(self, record):
        return "The configuration file at /etc/module-build-service/config.py was not present" not in record.getMessage()


logging.getLogger("module_build_service").addFilter(MBSWarningFilter())
