# -*- coding: utf-8 -*-

from __future__ import absolute_import

import atexit
import logging
import os
import path
import SimpleXMLRPCServer
import sched
import threading
import time

from fabric import context_managers
from fabric import decorators
from fabric import operations
from multiprocessing import pool

from watchdog import events
from watchdog import observers

from . import config

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(message)s')


CONFIG_FILENAME = '.watson.yaml'
DEFAULT_PROJECT_INDICATORS = [CONFIG_FILENAME, '.vip', 'setup.py']


class WatsonError(StandardError):
    pass


def find_project_directory(start=".", look_for=None):
    """Finds a directory that looks like a project directory.

    The search is performed up in the directory tree, and is finished when
    one of the terminators is found.

    Args:
        start: a path (directory) from where the search is started
            "." by default
        look_for: a list of search terminators,
            core.DEFAULT_PROJECT_INDICATORS by default

    Returns:
        A path to a directory that contains one of terminators

    Raises:
        WatsonError: when no such directory can be found
    """
    look_for = set(look_for or DEFAULT_PROJECT_INDICATORS)

    directory = path.path(start).abspath()

    while directory.parent != directory:
        items = os.listdir(directory)
        if any(i in look_for for i in items):
            return directory

        directory = directory.parent

    raise WatsonError('%s does not look like a project subdirectory' % start)


def get_project_name(working_dir):
    """Returns a project name from given working directory."""
    return path.path(working_dir).name


class EventScheduler(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self._sched = sched.scheduler(time.time, time.sleep)
        self._is_finished = False
        self._condition = threading.Condition()
        self._join_event = threading.Event()

    @property
    def is_finished(self):
        with self._condition:
            return self._is_finished

    def schedule(self, event, delay, function):
        with self._condition:
            logging.info('Scheduling %s in %ss', function.__name__, delay)
            self._condition.notify()

            if event is not None:
                self._sched.cancel(event)
            return self._sched.enter(delay, 1, function, [])

    def stop(self):
        with self._condition:
            logging.info('Stopping event scheduler')
            self._is_finished = True
            self._condition.notify()

    def join(self, timeout=None):
        self._join_event.wait(timeout)

    def run(self):
        logging.info('Starting event scheduler')

        while not self.is_finished:
            self._sched.run()
            with self._condition:
                if not self._is_finished:
                    logging.info('Queue is empty')
                    self._condition.wait()

        self._join_event.set()
        logging.info('Event scheduler stopped')


class ProjectWatcher(events.FileSystemEventHandler):

    # TODO(dejw): should expose some stats (like how many times it was
    #             notified) or how many times it succeeed in testing etc.

    def __init__(self, config, working_dir, scheduler, builder, observer):
        self._event = None

        self.name = get_project_name(working_dir)
        self.working_dir = working_dir
        self.set_config(config)

        self._last_status = (None, None)
        self._create_notification()

        self._scheduler = scheduler
        self._builder = builder
        self._observer = observer

        # TODO(dejw): allow to change observing patterns (and recursiveness)
        self._watch = observer.schedule(self, path=working_dir, recursive=True)

        logging.info('Observing %s', working_dir)

    @property
    def script(self):
        return self._config['script']

    def set_config(self, user_config):
        self._config = config.ProjectConfig(user_config)

    def shutdown(self, observer):
        observer.unschedule(self._watch)

    def on_any_event(self, event):
        logging.debug('Event: %s', repr(event))
        self.schedule_build()

    def schedule_build(self, timeout=None):
        """Schedules a building process in configured timeout."""

        if timeout is None:
            timeout = self._config['build_timeout']

        logging.debug('Scheduling a build in %ss', timeout)
        self._event = self._scheduler.schedule(
            self._event, timeout, self.build)

    def build(self):
        """Builds the project and shows notification on result."""
        logging.info('Building %s (%s)', self.name, self.working_dir)
        self._event = None
        status = self._builder.execute_script(self.working_dir, self.script)
        self._show_notification(status)

    def _create_notification(self):
        import pynotify
        self._notification = pynotify.Notification('')
        self._notification.set_timeout(5)

        # FIXME(dejw): should actually disable all projects and remove
        #      notifications
        atexit.register(self._hide_notification)

    def _hide_notification(self):
        self._notification.close()

    def _show_notification(self, status):
        succeeed, result = status
        output = '\n'.join([result.stdout.strip(), result.stderr.strip()])

        if not succeeed:
            self._notification.update(
                '%s failed' % self.name, output or "No output")
        else:
            self._notification.update('%s back to normal' % self.name,
                                      output)

        self._notification.show()
        self._last_status = status


class ProjectBuilder(object):

    def execute_script(self, working_dir, script):
        return self._execute_script_internal(working_dir, script)

    @decorators.with_settings(warn_only=True)
    def _execute_script_internal(self, working_dir, script):
        succeeded = True
        result = None

        with context_managers.lcd(working_dir):
            for command in script:
                result = operations.local(command, capture=True)
                succeeded = succeeded and result.succeeded
                if not succeeded:
                    break

        return (succeeded, result)


class WatsonServer(object):

    def __init__(self):
        self._projects = {}

        self._builder = ProjectBuilder()
        self._observer = observers.Observer()
        self._observer.start()

        self._scheduler = EventScheduler()
        self._scheduler.start()

        self._init_pynotify()

        # TODO(dejw): read (host, port) from config in user's directory
        self.endpoint = ('localhost', 0x221B)
        self._api = SimpleXMLRPCServer.SimpleXMLRPCServer(
            self.endpoint, allow_none=True)
        self._api.register_instance(self)

    def _start(self):
        logging.info('Server listening on %s' % (self.endpoint,))
        self._api.serve_forever()

    def _join(self):
        self._api.shutdown()

    def _init_pynotify(self):
        logging.info('Configuring pynotify')
        import pynotify
        pynotify.init('Watson')
        assert pynotify.get_server_caps() is not None

    def hello(self):
        return 'World!'

    def shutdown(self):
        logging.info('Shuting down')
        self._api.server_close()
        self._observer.stop()
        self._scheduler.stop()

        self._observer.join()
        self._scheduler.join()

    def add_project(self, working_dir, config):
        logging.info('Adding a project: %s', working_dir)

        project_name = get_project_name(working_dir)

        if project_name not in self._projects:
            self._projects[project_name] = ProjectWatcher(
                config, working_dir, self._scheduler, self._builder,
                self._observer)

        else:
            self._projects[project_name].set_config(config)

        self._projects[project_name].schedule_build(0)
