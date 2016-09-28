# -*- coding: utf-8 -*-
"""Kyco - Kytos Contoller

This module contains the main class of Kyco, which is
:class:`~.controller.Controller`.

Basic usage:

.. code-block:: python3

    from kyco.config import KycoConfig
    from kyco.controller import Controller
    config = KycoConfig()
    controller = Controller(config.options)
    controller.start()
"""

import os
import re
from importlib.machinery import SourceFileLoader
from socket import error as SocketError
from threading import Thread

from kyco.core.buffers import KycoBuffers
from kyco.core.events import KycoEvent
from kyco.core.switch import Connection

#(KycoConnectionLost, KycoError, KycoEvent,
#                              KycoNewConnection, KycoShutdownEvent,
#                              KycoSwitchDown)

from kyco.core.exceptions import KycoSwitchOfflineException
from kyco.core.tcp_server import KycoOpenFlowRequestHandler, KycoServer
from kyco.utils import KycoCoreNApp, start_logger

log = start_logger()

__all__ = ('Controller',)


class Controller(object):
    """This is the main class of Kyco.

    The main responsabilities of this class are:
        - start a thread with :class:`~.core.tcp_server.KycoServer`;
        - manage KycoNApps (install, load and unload);
        - keep the buffers (instance of :class:`~.core.buffers.KycoBuffers`);
        - manage which event should be sent to NApps methods;
        - manage the buffers handlers, considering one thread per handler.
    """
    def __init__(self, options):
        """Init method of Controller class.

        Args:
            options (ParseArgs.args): 'options' attribute from an instance of
                KycoConfig class
        """
        #: dict: keep the main threads of the controller (buffers and handler)
        self._threads = {}
        #: KycoBuffers: KycoBuffer object with Controller buffers
        self.buffers = KycoBuffers()
        #: dict: keep track of the socket connections labeled by ``(ip, port)``
        #:
        #: This dict stores all connections between the controller and the
        #: swtiches. The key for this dict is a tuple (ip, port). The content is
        #: another dict with the connection information.
        self.connections = {}
        #: dict: mapping of events and event listeners.
        #:
        #: The key of the dict is a KycoEvent (or a string that represent a
        #: regex to match agains KycoEvents) and the value is a list of methods
        #: that will receive the referenced event
        self.events_listeners = {'kyco/core.connection.new': [self.new_connection]}
#                                 'KycoRawError': [self.raw_error]}
        #: dict: Current loaded apps - 'napp_name': napp (instance)
        #:
        #: The key is the napp name (string), while the value is the napp
        #: instance itself.
        self.napps = {}
        #: Object generated by ParseArgs on config.py file
        self.options = options
        #: KycoServer: Instance of KycoServer that will be listening to TCP
        #: connections.
        self.server = None
        #: dict: Current existing switches.
        #:
        #: The key is the switch dpid, while the value is a KycoSwitch object.
        self.switches = {}  # dpid: KycoSwitch()

    def start(self):
        """Start the controller.

        Starts a thread with the KycoServer (TCP Server).
        Starts a thread for each buffer handler.
        Load the installed apps.
        """
        log.info("Starting Kyco - Kytos Controller")
        self.server = KycoServer((self.options.listen,
                                  int(self.options.port)),
                                 KycoOpenFlowRequestHandler,
                                 # TODO: Change after #62 definitions
                                 #       self.buffers.raw.put)
                                 self)

        thrds = {'tcp_server': Thread(name='TCP server',
                                      target=self.server.serve_forever),
                 'raw_event_handler': Thread(name='RawEvent Handler',
                                             target=self.raw_event_handler),
#                 'msg_in_event_handler': Thread(name='MsgInEvent Handler',
#                                                target=self.msg_in_event_handler),
#                 'msg_out_event_handler': Thread(name='MsgOutEvent Handler',
#                                                 target=self.msg_out_event_handler),
                 'app_event_handler': Thread(name='AppEvent Handler',
                                             target=self.app_event_handler)}

        self._threads = thrds
        for thread in self._threads.values():
            thread.start()

        log.info("Loading kyco apps...")
        self.load_napps()

    def stop(self):
        """Stops the controller.

        This method should:
            - announce on the network that the controller will shutdown;
            - stop receiving incoming packages;
            - call the 'shutdown' method of each KycoNApp that is running;
            - finish reading the events on all buffers;
            - stop each running handler;
            - stop all running threads;
            - stop the KycoServer;
        """
        log.info("Stopping Kyco")
        self.server.socket.close()
        self.server.shutdown()
        self.buffers.send_stop_signal()

        self.unload_napps()

        for thread in self._threads.values():
            log.info("Stopping thread: %s", thread.name)
            thread.join()

        for thread in self._threads.values():
            while thread.is_alive():
                pass


    def notify_listeners(self, event):
        """Sends the event to the specified listeners.

        Loops over self.events_listeners matching (by regexp) the attribute name
        of the event with the keys of events_listeners. If a match occurs, then
        send the event to each registered listener.

        Args:
            event (KycoEvent): An instance of a KycoEvent.
        """
        for event_regex, listeners in self.events_listeners.items():
            if re.match(event_regex, event.name):
                for listener in listeners:
                    listener(event)

    def raw_event_handler(self):
        """Handle raw events.

        This handler listen to the raw_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.

        It also verify if there is a switch instantiated on that connection_id
        `(ip, port)`. If a switch was found, then the `connection_id` attribute
        is set to `None` and the `dpid` is replaced with the switch dpid.
        """
        log.info("Raw Event Handler started")
        while True:
            event = self.buffers.raw.get()

            if event.name == "kyco/core.shutdown":
                log.debug("RawEvent handler stopped")
                break

            self.notify_listeners(event)

#    def msg_in_event_handler(self):
#        """Handle msg_in events.
#
#        This handler listen to the msg_in_buffer, get every event added to this
#        buffer and sends it to the listeners listening to this event.
#        """
#        log.info("Message In Event Handler started")
#        while True:
#            event = self.buffers.msg_in.get()
#
#            if event.name == "kyco/core.shutdown":
#                log.debug("MsgInEvent handler stopped")
#                break
#
#            log.debug("MsgInEvent handler called")
#            # Sending the event to the listeners
#            self.notify_listeners(event)

#    def msg_out_event_handler(self):
#        """Handle msg_out events.
#
#        This handler listen to the msg_out_buffer, get every event added to
#        this buffer and sends it to the listeners listening to this event.
#        """
#        log.info("Message Out Event Handler started")
#        while True:
#            triggered_event = self.buffers.msg_out.get()
#
#            if triggered_event.name == "kyco/core.shutdown":
#                log.debug("MsgOutEvent handler stopped")
#                break
#
#            log.debug("MsgOutEvent handler called")
#            dpid = triggered_event.dpid
#            connection_id = triggered_event.connection_id
#            message = triggered_event.content['message']
#
#            # Checking if we need to send the message to a switch (based on its
#            # dpid) or to a connection (based on connection_id).
#            if dpid is not None:
#                # Sending the OpenFlow message to the switch
#                destination = dpid
#            else:
#                destination = connection_id
#
#            try:
#                self.send_to(destination, message.pack())
#                # Sending the event to the listeners
#                self.notify_listeners(triggered_event)
#            except (OSError, SocketError, KycoSwitchOfflineException) as excp:
#                content = {'event': triggered_event,
#                           'execption': excp,
#                           'destination': triggered_event.destination}
#
#                event = KycoEvent(name='kytos/core.error',
#                                  content = content)
#
#                self.buffers.app.put(event)

    def app_event_handler(self):
        """Handle app events.

        This handler listen to the app_buffer, get every event added to this
        buffer and sends it to the listeners listening to this event.
        """
        log.info("App Event Handler started")
        while True:
            event = self.buffers.app.get()

            log.debug("AppEvent handler called")
            # Sending the event to the listeners
            self.notify_listeners(event)

            if event.name == "kyco/core.shutdown":
                log.debug("AppEvent handler stopped")
                break

#    def raw_error(self, event):
#        """Unwrapp KycoRawError message.
#
#        When any error occurs on the tcp_handler module, it will send a
#        KycoRawError event to the raw_buffer, since it only have access to this
#        buffer. Then, this KycoRawError event will be passed to this method
#        that will get the error and put it on the app_buffer as a KycoError
#        event, so every napp can be notified (if it is listening this event).
#        """
#        event = event.content['event']
#        self.buffers.app.put(event)

    def get_switch_by_dpid(self, dpid):
        try:
            return self.switches[dpid]
        except KeyError:
            return None

    def get_connection_by_id(self, id):
        try:
            return self.connections[id]
        except KeyError:
            return None

    def remove_connection(self, id):
        try:
            connection = self.connections.pop(id)
        except KeyError:
            return False

        return True

    def remove_switch(self, dpid):
        try:
            switch = self.switches.pop(dpid)
        except KeyError:
            return False

# KycoError          = kytos/core.error
# KycoNewConnection  = kytos/core.connection.new


    def new_connection(self, event):
        """Handle a kytos/core.connection.new event.

        This method will read new connection event and store the connection
        (socket) into the connections attribute on the controller.

        It also clear all references to the connection since it is a new
        connection on the same ip:port.

        Args:
            event (KycoEvent): The received event (kytos/core.connection.new)
            with the needed infos.
        """

        log.info("Handling KycoEvent:kytos/core.connection.new ...")

        connection = event.content['connection']

        # Remove old connection (aka cleanup) if exists
        if self.get_connection_by_id(connection.id):
            self.remove_connection(connection.id)

        # Disconnect old switch if exists
        switch = self.get_switch_by_dpid(connection.dpid)
        if switch:
            switch.disconnect()

        # Update connections with the new connection
        self.connections[connection.id] = connection

#    def add_new_switch(self, switch):
#        """Adds a new switch on the controller.
#
#        Args:
#            switch (KycoSwitch): A KycoSwitch object
#        """
#
#        self.switches[switch.dpid] = switch
#        self.connections[switch.connection_id]['socket'] = switch.socket
#        self.connections[switch.connection_id]['dpid'] = switch.dpid

#    def connection_lost(self, event):
#        """Handle a ConnectionLost event.
#
#        This method will read the event and change the switch that has been
#        disconnected.
#
#        At last, it will create and send a SwitchDown event to the app buffer.
#
#        Args:
#            event (KycoConnectionLost): Received event with the needed infos
#        """
#        log.info("Handling KycoConnectionLost event for: %s",
#                 event.connection_id)
#        if event.connection_id in self.connections:
#            self.remove_connection(event.connection_id)
#
#        if event.dpid in self.switches:
#            self.disconnect_switch(event.dpid)

#    def remove_connection(self, connection_id):
#        """Purge data related to a connection_id.
#
#        This will close sockets, remove the 'connection_id' from the
#        self.connections dict and also 'disconnect' the switch attached
#        to the connection.
#        """
#        if connection_id not in self.connections:
#            msg = "Connection {} not found on Kyco".format(connection_id)
#            raise Exception(msg)
#
#        connection = self.connections.pop(connection_id)
#        if 'dpid' in connection:
#            dpid = connection.pop('dpid')
#            if dpid in self.switches:
#                self.disconnect_switch(dpid)
#        if 'socket' in connection:
#            socket = connection.pop('socket')
#            if socket is not None:
#                try:
#                    socket.close()
#                except:
#                    pass
#
#    def disconnect_switch(self, dpid):
#        """End the connection with a switch.
#
#        If no switch with the specified dpid is passed, an exception is raised.
#        Args:
#            dpid: the dpid of the switch
#        """
#
#        if dpid not in self.switches:
#            raise Exception("Switch {} not found on Kyco".format(dpid))
#
#        switch = self.switches.pop(dpid)
#        connection_id = switch.connection_id
#        switch.disconnect()
#
#        if connection_id in self.connections:
#            self.remove_connection(connection_id)
#
#        new_event = KycoSwitchDown(dpid=dpid)
#
#        self.buffers.app.put(new_event)

    def send_to(self, destination, message):
        """Send a packed OF message to the client/destination

        If the destination is a dpid (string), then the method will look for
        a switch by it's dpid.
        If the destination is a connection_id (tuple), then the method will
        look for the related connection to send the message.

        Args:
            destination (): It could be a connection_id (tuple) or a switch
                dpid (string)
            message (bytes): packed openflow message (binary)
        """
        if isinstance(destination, tuple):
            try:
                self.connections[destination]['socket'].send(message)
            except (OSError, SocketError) as exception:
                # TODO: Raise a ConnectionLost event?
                err_msg = 'Error while sending a message to connection %s'
                log.debug(err_msg, destination)
                raise exception
        else:
            try:
                self.switches[destination].send(message)
            except (OSError, SocketError, KycoSwitchOfflineException) as excp:
                err_msg = 'Error while sending a message to switch %s'
                log.debug(err_msg, destination)
                raise excp

    def load_napp(self, napp_name):
        """Load a single app.

        Load a single NAPP based on its name.
        Args:
            napp_name (str): Name of the NApp to be loaded.
        """
        path = os.path.join(self.options.napps, napp_name, 'main.py')
        module = SourceFileLoader(napp_name, path)

        napp = module.load_module().Main(controller=self)
        self.napps[napp_name] = napp

        for event_type, listeners in napp._listeners.items():
            if event_type not in self.events_listeners:
                self.events_listeners[event_type] = []
            self.events_listeners[event_type].extend(listeners)

        napp.start()

    def install_napp(self, napp_name):
        """Install the requested NApp by its name.

        Downloads the NApps from the NApp network and install it.
        TODO: Download or git-clone?

        Args:
            napp_name (str): Name of the NApp to be installed.
        """
        pass

    def load_napps(self):
        """Load all NApps installed on the NApps dir"""
        napps_dir = self.options.napps
        try:
            for author in os.listdir(napps_dir):
                author_dir = os.path.join(napps_dir, author)
                for napp_name in os.listdir(author_dir):
                    full_name = "{}/{}".format(author, napp_name)
                    log.info("Loading app %s", full_name)
                    self.load_napp(full_name)
        except FileNotFoundError as e:
            log.error("Could not load napps: {}".format(e))

    def unload_napp(self, napp_name):
        """Unload a specific NApp based on its name.

        Args:
            napp_name (str): Name of the NApp to be unloaded.
        """
        napp = self.napps.pop(napp_name)
        napp.shutdown()
        # Removing listeners from that napp
        for event_type, listeners in napp._listeners.items():
            for listener in listeners:
                self.events_listeners[event_type].remove(listener)
            if len(self.events_listeners[event_type]) == 0:
                self.events_listeners.pop(event_type)

    def unload_napps(self):
        """Unload all loaded NApps that are not core NApps."""
        # list() is used here to avoid the error:
        # 'RuntimeError: dictionary changed size during iteration'
        # This is caused by looping over an dictionary while removing
        # items from it.
        for napp_name in list(self.napps):
            if not isinstance(self.napps[napp_name], KycoCoreNApp):
                self.unload_napp(napp_name)
