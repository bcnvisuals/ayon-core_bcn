import os

import logging

from avalon import api as avalon  # pipeline, houdini
from pyblish import api as pyblish


PARENT_DIR = os.path.dirname(__file__)
PACKAGE_DIR = os.path.dirname(PARENT_DIR)
PLUGINS_DIR = os.path.join(PACKAGE_DIR, "plugins")

PUBLISH_PATH = os.path.join(PLUGINS_DIR, "houdini", "publish")
LOAD_PATH = os.path.join(PLUGINS_DIR, "houdini", "load")
CREATE_PATH = os.path.join(PLUGINS_DIR, "houdini", "create")

log = logging.getLogger("colorbleed.houdini")


def install():

    pyblish.register_plugin_path(PUBLISH_PATH)
    avalon.register_plugin_path(avalon.Loader, LOAD_PATH)
    avalon.register_plugin_path(avalon.Creator, CREATE_PATH)

    log.info("Installing callbacks ... ")
    avalon.on("init", on_init)
    avalon.on("save", on_save)
    avalon.on("open", on_open)

    log.info("Overriding existing event 'taskChanged'")

    log.info("Setting default family states for loader..")
    avalon.data["familiesStateToggled"] = ["colorbleed.imagesequence"]


def on_init():
    pass


def on_save():
    pass


def on_open():
    pass


def on_task_changed(*args):
    """Wrapped function of app initialize and maya's on task changed"""
    pass