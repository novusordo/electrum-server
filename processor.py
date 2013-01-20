import json
import Queue as queue
import socket
import threading
import time
import traceback
import sys

from utils import random_string, timestr, print_log


class Shared:

    def __init__(self, config):
        self.lock = threading.Lock()
        self._stopped = False
        self.config = config

    def stop(self):
        print_log("Stopping Stratum")
        with self.lock:
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped


class Processor(threading.Thread):

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.dispatcher = None
        self.queue = queue.Queue()

    def process(self, request):
        pass

    def add_request(self, request):
        self.queue.put(request)

    def push_response(self, response):
        #print "response", response
        self.dispatcher.request_dispatcher.push_response(response)

    def run(self):
        while not self.shared.stopped():
            request = self.queue.get(10000000000)
            try:
                self.process(request)
            except:
                traceback.print_exc(file=sys.stdout)

        print_log("processor terminating")


class Dispatcher:

    def __init__(self, config):
        self.shared = Shared(config)
        self.request_dispatcher = RequestDispatcher(self.shared)
        self.request_dispatcher.start()
        self.response_dispatcher = \
            ResponseDispatcher(self.shared, self.request_dispatcher)
        self.response_dispatcher.start()

    def register(self, prefix, processor):
        processor.dispatcher = self
        processor.shared = self.shared
        processor.start()
        self.request_dispatcher.processors[prefix] = processor


class RequestDispatcher(threading.Thread):

    def __init__(self, shared):
        self.shared = shared
        threading.Thread.__init__(self)
        self.daemon = True
        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()
        self.internal_ids = {}
        self.internal_id = 1
        self.lock = threading.Lock()
        self.sessions = []
        self.processors = {}

    def push_response(self, item):
        self.response_queue.put(item)

    def pop_response(self):
        return self.response_queue.get()

    def push_request(self, session, item):
        self.request_queue.put((session, item))

    def pop_request(self):
        return self.request_queue.get()

    def get_session_by_address(self, address):
        for x in self.sessions:
            if x.address == address:
                return x

    def get_session_id(self, internal_id):
        with self.lock:
            return self.internal_ids.pop(internal_id)

    def store_session_id(self, session, msgid):
        with self.lock:
            self.internal_ids[self.internal_id] = session, msgid
            r = self.internal_id
            self.internal_id += 1
            return r

    def run(self):
        if self.shared is None:
            raise TypeError("self.shared not set in Processor")
        while not self.shared.stopped():
            session, request = self.pop_request()
            try:
                self.do_dispatch(session, request)
            except:
                traceback.print_exc(file=sys.stdout)

        self.stop()

    def stop(self):
        pass

    def do_dispatch(self, session, request):
        """ dispatch request to the relevant processor """

        method = request['method']
        params = request.get('params', [])
        suffix = method.split('.')[-1]

        if session is not None:
            is_new = session.protocol_version >= 0.5
            if suffix == 'subscribe':
                session.subscribe_to_service(method, params)

        # store session and id locally
        request['id'] = self.store_session_id(session, request['id'])

        prefix = request['method'].split('.')[0]
        try:
            p = self.processors[prefix]
        except:
            print_log("error: no processor for", prefix)
            return

        p.add_request(request)

        if method in ['server.version']:
            session.version = params[0]
            try:
                session.protocol_version = float(params[1])
            except:
                pass

            #if session.protocol_version < 0.6:
            #    print_log("stopping session from old client", session.protocol_version)
            #    session.stop()

    def get_sessions(self):
        with self.lock:
            r = self.sessions[:]
        return r

    def add_session(self, session):
        with self.lock:
            self.sessions.append(session)

    def collect_garbage(self):
        # Deep copy entire sessions list and blank it
        # This is done to minimise lock contention
        with self.lock:
            sessions = self.sessions[:]
            self.sessions = []
        for session in sessions:
            if not session.stopped():
                # If session is still alive then re-add it back
                # to our internal register
                self.add_session(session)


class Session:

    def __init__(self):
        self._stopped = False
        self.lock = threading.Lock()
        self.subscriptions = []
        self.address = ''
        self.name = ''
        self.version = 'unknown'
        self.protocol_version = 0.
        self.time = time.time()
        threading.Timer(2, self.info).start()

    # Debugging method. Doesn't need to be threadsafe.
    def info(self):
        for sub in self.subscriptions:
            #print sub
            method = sub[0]
            if method == 'blockchain.address.subscribe':
                addr = sub[1]
                break
        else:
            addr = None

        if self.subscriptions:
            print_log("%4s" % self.name,
                      "%15s" % self.address,
                      "%35s" % addr,
                      "%3d" % len(self.subscriptions),
                      self.version)

    def stopped(self):
        with self.lock:
            return self._stopped

    def subscribe_to_service(self, method, params):
        subdesc = self.build_subdesc(method, params)
        with self.lock:
            if subdesc is not None:
                self.subscriptions.append(subdesc)

    # subdesc = A subscription description
    @staticmethod
    def build_subdesc(method, params):
        if method == "blockchain.numblocks.subscribe":
            return method,
        elif method == "blockchain.headers.subscribe":
            return method,
        elif method in ["blockchain.address.subscribe"]:
            if not params:
                return None
            else:
                return method, params[0]
        else:
            return None

    def contains_subscription(self, subdesc):
        with self.lock:
            return subdesc in self.subscriptions


class ResponseDispatcher(threading.Thread):

    def __init__(self, shared, request_dispatcher):
        self.shared = shared
        self.request_dispatcher = request_dispatcher
        threading.Thread.__init__(self)
        self.daemon = True

    def run(self):
        while not self.shared.stopped():
            self.update()

    def update(self):
        response = self.request_dispatcher.pop_response()
        #print "pop response", response
        internal_id = response.get('id')
        method = response.get('method')
        params = response.get('params')

        # A notification
        if internal_id is None:  # and method is not None and params is not None:
            found = self.notification(method, params, response)
            if not found and method == 'blockchain.address.subscribe':
                request = {
                    'id': None,
                    'method': method.replace('.subscribe', '.unsubscribe'),
                    'params': [self.shared.config.get('server', 'password')] + params,
                }

                self.request_dispatcher.push_request(None, request)
        # A response
        elif internal_id is not None:
            self.send_response(internal_id, response)
        else:
            print_log("no method", response)

    def notification(self, method, params, response):
        subdesc = Session.build_subdesc(method, params)
        found = False
        for session in self.request_dispatcher.sessions:
            if session.stopped():
                continue
            if session.contains_subscription(subdesc):
                session.send_response(response)
                found = True
        # if not found: print_log("no subscriber for", subdesc)
        return found

    def send_response(self, internal_id, response):
        session, message_id = self.request_dispatcher.get_session_id(internal_id)
        if session:
            response['id'] = message_id
            session.send_response(response)
        #else:
        #    print_log("send_response: no session", message_id, internal_id, response )
