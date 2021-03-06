# container-service-extension
# Copyright (c) 2017 VMware, Inc. All Rights Reserved.
# SPDX-License-Identifier: BSD-2-Clause

import platform
import signal
import sys
import threading
from threading import Thread
import time
import traceback

import click
import pkg_resources
from pyvcloud.vcd.client import BasicLoginCredentials
from pyvcloud.vcd.client import Client
import requests

from container_service_extension.configure_cse import check_cse_installation
from container_service_extension.configure_cse import get_validated_config
from container_service_extension.consumer import MessageConsumer
from container_service_extension.logger import configure_server_logger
from container_service_extension.logger import SERVER_DEBUG_LOG_FILEPATH
from container_service_extension.logger import SERVER_DEBUG_WIRELOG_FILEPATH
from container_service_extension.logger import SERVER_INFO_LOG_FILEPATH
from container_service_extension.logger import SERVER_LOGGER as LOGGER
from container_service_extension.utils import connect_vcd_user_via_token
from container_service_extension.utils import SYSTEM_ORG_NAME


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(
                *args, **kwargs)
        return cls._instances[cls]


def signal_handler(signal, frame):
    print('\nCrtl+C detected, exiting')
    raise KeyboardInterrupt()


def consumer_thread(c):
    try:
        LOGGER.info('About to start consumer_thread %s.', c)
        c.run()
    except Exception:
        click.echo('About to stop consumer_thread.')
        LOGGER.error(traceback.format_exc())
        c.stop()


class Service(object, metaclass=Singleton):
    def __init__(self, config_file, should_check_config=True):
        self.config_file = config_file
        self.config = None
        self.should_check_config = should_check_config
        self.is_enabled = False
        self.consumers = []
        self.threads = []
        self.should_stop = False

    def get_service_config(self):
        return self.config

    def get_sys_admin_client(self):
        if self.config is not None:
            if not self.config['vcd']['verify']:
                LOGGER.warning('InsecureRequestWarning: Unverified HTTPS '
                               'request is being made. Adding certificate '
                               'verification is strongly advised.')
                requests.packages.urllib3.disable_warnings()
            client = Client(
                uri=self.config['vcd']['host'],
                api_version=self.config['vcd']['api_version'],
                verify_ssl_certs=self.config['vcd']['verify'],
                log_file=SERVER_DEBUG_WIRELOG_FILEPATH,
                log_requests=True,
                log_headers=True,
                log_bodies=True)
            credentials = BasicLoginCredentials(self.config['vcd']['username'],
                                                SYSTEM_ORG_NAME,
                                                self.config['vcd']['password'])
            client.set_credentials(credentials)
            return client
        return None

    def active_requests_count(self):
        n = 0
        for t in threading.enumerate():
            from container_service_extension.broker import DefaultBroker
            if type(t) == DefaultBroker:
                n += 1
        return n

    def get_status(self):
        if self.is_enabled:
            return 'Running'
        else:
            if self.should_stop:
                return 'Shutting down'
            else:
                return 'Disabled'

    def info(self, headers):
        tenant_client, session = connect_vcd_user_via_token(
            vcd_uri=self.config['vcd']['host'],
            headers=headers,
            verify_ssl_certs=self.config['vcd']['verify'])
        result = Service.version()
        if tenant_client.is_sysadmin():
            result['consumer_threads'] = len(self.threads)
            result['all_threads'] = threading.activeCount()
            result['requests_in_progress'] = self.active_requests_count()
            result['config_file'] = self.config_file
            result['status'] = self.get_status()
        else:
            del result['python']
        return result

    @classmethod
    def version(cls):
        ver = pkg_resources.require('container-service-extension')[0].version
        ver_obj = {
            'product': 'CSE',
            'description': 'Container Service Extension for VMware vCloud '
                           'Director',
            'version': ver,
            'python': platform.python_version()
        }
        return ver_obj

    def update_status(self, headers, body):
        tenant_client, session = connect_vcd_user_via_token(
            vcd_uri=self.config['vcd']['host'],
            headers=headers,
            verify_ssl_certs=self.config['vcd']['verify'])

        reply = {}
        if tenant_client.is_sysadmin():
            if 'enabled' in body:
                if body['enabled'] and self.should_stop:
                    reply['body'] = {
                        'message': 'Cannot enable while being stopped.'
                    }
                    reply['status_code'] = 500
                else:
                    self.is_enabled = body['enabled']
                    reply['body'] = {'message': 'Updated'}
                    reply['status_code'] = 200
            elif 'stopped' in body:
                if self.is_enabled:
                    reply['body'] = {
                        'message':
                        'Cannot stop CSE while is enabled.'
                        ' Disable the service first.'
                    }
                    reply['status_code'] = 500
                else:
                    message = 'CSE graceful shutdown started.'
                    n = self.active_requests_count()
                    if n > 0:
                        message += ' CSE will finish processing %s requests.' \
                            % n
                    reply['body'] = {'message': message}
                    reply['status_code'] = 200
                    self.should_stop = True
            else:
                reply['body'] = {'message': 'Unknown status'}
                reply['status_code'] = 500
        else:
            reply['body'] = {'message': 'Unauthorized'}
            reply['status_code'] = 401
        return reply

    def run(self):
        self.config = get_validated_config(self.config_file)
        if self.should_check_config:
            check_cse_installation(self.config)

        configure_server_logger()

        message = f"Container Service Extension for vCloudDirector" \
                  f"\nServer running using config file: {self.config_file}" \
                  f"\nLog files: {SERVER_INFO_LOG_FILEPATH}, " \
                  f"{SERVER_DEBUG_LOG_FILEPATH}" \
                  f"\nwaiting for requests (ctrl+c to close)"

        signal.signal(signal.SIGINT, signal_handler)
        click.secho(message)
        LOGGER.info(message)

        amqp = self.config['amqp']
        num_consumers = self.config['service']['listeners']

        for n in range(num_consumers):
            try:
                c = MessageConsumer(
                    amqp['host'], amqp['port'], amqp['ssl'], amqp['vhost'],
                    amqp['username'], amqp['password'], amqp['exchange'],
                    amqp['routing_key'])
                name = 'MessageConsumer-%s' % n
                t = Thread(name=name, target=consumer_thread, args=(c, ))
                t.daemon = True
                t.start()
                LOGGER.info('started thread %s', t.ident)
                self.threads.append(t)
                self.consumers.append(c)
                time.sleep(0.25)
            except KeyboardInterrupt:
                break
            except Exception:
                print(traceback.format_exc())

        LOGGER.info('num of threads started: %s', len(self.threads))

        self.is_enabled = True

        while True:
            try:
                time.sleep(1)
                if self.should_stop and self.active_requests_count() == 0:
                    break
            except KeyboardInterrupt:
                break
            except Exception:
                click.secho(traceback.format_exc())
                sys.exit(1)

        LOGGER.info('stop detected')
        LOGGER.info('closing connections...')
        for c in self.consumers:
            try:
                c.stop()
            except Exception:
                pass
        LOGGER.info('done')
