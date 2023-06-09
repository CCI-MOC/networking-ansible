# Copyright (c) 2018 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import os

from neutron.db import provisioning_blocks
from neutron.objects.network import Network
from neutron.objects.network import NetworkSegment
from neutron.objects.ports import Port
from neutron.objects.trunk import Trunk
from neutron.plugins.ml2.common import exceptions as ml2_exc
from neutron_lib.api.definitions import portbindings
from neutron_lib.api.definitions import provider_net
from neutron_lib.callbacks import resources
from neutron_lib.plugins.ml2 import api as ml2api
from oslo_config import cfg
from oslo_log import log as logging

from networking_ansible import config
from networking_ansible import constants as c
from networking_ansible import exceptions
from networking_ansible.ml2 import trunk_driver

from network_runner import api as net_runr_api
from network_runner.models.inventory import Inventory

from tooz import coordination

LOG = logging.getLogger(__name__)
CONF = config.CONF


class AnsibleMechanismDriver(ml2api.MechanismDriver):
    """ML2 Mechanism Driver for Ansible Networking

    https://www.ansible.com/integrations/networks
    """

    def initialize(self):
        LOG.debug("Initializing Ansible ML2 driver")

        # Get ML2 config
        self.ml2config = config.Config()

        # Build a network runner inventory object
        # and instatiate network runner
        _inv = Inventory()
        _inv.deserialize({'all': {'hosts': self.ml2config.inventory}})
        self.net_runr = net_runr_api.NetworkRunner(_inv)

        # build the custom params and extra params dict.
        # this holds kwargs per host to pass to network runner
        self.kwargs = {}
        for host_name in self.ml2config.inventory:
            self.kwargs[host_name] = {}
            for key, val in self.ml2config.inventory[host_name].items():
                if key in c.EXTRA_PARAMS or \
                        key.startswith(c.CUSTOM_PARAM_PREFIX):
                    if key.startswith(c.CUSTOM_PARAM_PREFIX):
                        key = key[len(c.CUSTOM_PARAM_PREFIX):]
                    self.kwargs[host_name][key] = val

        self.coordinator = coordination.get_coordinator(
            cfg.CONF.ml2_ansible.coordination_uri,
            '{}-{}'.format(CONF.host, os.getpid()))

        # the heartbeat will have the default timeout of 30 seconds
        # that can be changed per-driver. Both Redis and etcd drivers
        # use 30 second timeouts.
        self.coordinator.start(start_heart=True)
        LOG.debug("Ansible ML2 coordination started via uri %s",
                  cfg.CONF.ml2_ansible.coordination_uri)

        self.trunk_driver = trunk_driver.NetAnsibleTrunkDriver.create(self)

    def create_network_postcommit(self, context):
        """Create a network.

        :param context: NetworkContext instance describing the new
        network.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Raising an exception will
        cause the deletion of the resource.
        """

        # assuming all hosts
        # TODO(radez): can we filter by physnets?
        # TODO(michchap): we should be able to do each switch in parallel
        # if it becomes a performance issue. switch topology might also
        # open up options for filtering.
        for host_name in self.ml2config.inventory:
            host = self.ml2config.inventory[host_name]
            if host.get('manage_vlans', True):

                network = context.current
                network_id = network['id']
                provider_type = network[provider_net.NETWORK_TYPE]
                segmentation_id = network[provider_net.SEGMENTATION_ID]

                lock = self.coordinator.get_lock(host_name)
                with lock:
                    if provider_type == 'vlan' and segmentation_id:

                        # re-request network info in case it's stale
                        net = Network.get_object(context._plugin_context,
                                                 id=network_id)
                        LOG.debug('network create object: {}'.format(net))

                        # network was since deleted by user and we can discard
                        # this request
                        if not net:
                            return

                        # check the vlan for this request is still associated
                        # with this network. We don't currently allow updating
                        # the segment on a network - it's disallowed at the
                        # neutron level for provider networks - but that could
                        # change in the future
                        s_ids = [s.segmentation_id for s in net.segments]
                        if segmentation_id not in s_ids:
                            return

                        # Create VLAN on the switch
                        try:
                            self.net_runr.create_vlan(
                                host_name,
                                segmentation_id,
                                **self.kwargs[host_name])
                            LOG.info('Network {net_id}, segmentation '
                                     '{seg} has been added on '
                                     'ansible host {host}'.format(
                                         net_id=network['id'],
                                         seg=segmentation_id,
                                         host=host_name))

                        except Exception as e:
                            # TODO(radez) I don't think there is a message
                            #             returned from ansible runner's
                            #             exceptions
                            LOG.error('Failed to create network {net_id} '
                                      'on ansible host: {host}, '
                                      'reason: {err}'.format(
                                          net_id=network_id,
                                          host=host_name,
                                          err=e))
                            raise exceptions.NetworkingAnsibleMechException(e)

    def delete_network_postcommit(self, context):
        """Delete a network.

        :param context: NetworkContext instance describing the current
        state of the network, prior to the call to delete it.

        Called after the transaction commits. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance. Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """

        # assuming all hosts
        # TODO(radez): can we filter by physnets?
        for host_name in self.ml2config.inventory:
            host = self.ml2config.inventory[host_name]

            network = context.current
            provider_type = network[provider_net.NETWORK_TYPE]
            segmentation_id = network[provider_net.SEGMENTATION_ID]
            physnet = network[provider_net.PHYSICAL_NETWORK]

            if provider_type == 'vlan' and segmentation_id:
                if host.get('manage_vlans', True):
                    lock = self.coordinator.get_lock(host_name)
                    with lock:
                        # Find out if this segment is active.
                        # We need to find out if this segment is being used
                        # by another network before deleting it from the switch
                        # since reordering could mean that a vlan is recycled
                        # by the time this request is satisfied. Getting
                        # the current network is not enough
                        db = context._plugin_context

                        segments = NetworkSegment.get_objects(
                            db, segmentation_id=segmentation_id)

                        for segment in segments:
                            if segment.segmentation_id == segmentation_id and \
                               segment.physical_network == physnet and \
                               segment.network_type == 'vlan':
                                LOG.debug('Not deleting segment {} from {}'
                                          'because it was recreated'.format(
                                              segmentation_id, physnet))
                                return

                        # Delete VLAN on the switch
                        try:
                            self.net_runr.delete_vlan(
                                host_name,
                                segmentation_id,
                                **self.kwargs[host_name])
                            LOG.info('Network {net_id} has been deleted on '
                                     'ansible host {host}'.format(
                                         net_id=network['id'],
                                         host=host_name))

                        except Exception as e:
                            LOG.error('Failed to delete network {net} '
                                      'on ansible host: {host}, '
                                      'reason: {err}'.format(net=network['id'],
                                                             host=host_name,
                                                             err=e))
                            raise exceptions.NetworkingAnsibleMechException(e)

    def update_port_postcommit(self, context):
        """Update a port.

        :param context: PortContext instance describing the new
        state of the port, as well as the original state prior
        to the update_port call.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Raising an exception will
        result in the deletion of the resource.
        update_port_postcommit is called for all changes to the port
        state. It is up to the mechanism driver to ignore state or
        state changes that it does not know or care about.
        """
        # Handle VM ports
        if self._is_port_normal(context.current):
            port = context.current
            network = context.network.current
            mappings, segmentation_id = self.get_switch_meta(port, network)

            for switch_name, switch_port in mappings:
                LOG.debug('Ensuring Updated port {switch_port} on network '
                          '{network} {switch_name} to vlan: '
                          '{segmentation_id}'.format(
                              switch_port=switch_port,
                              network=network,
                              switch_name=switch_name,
                              segmentation_id=segmentation_id))

                self.ensure_port(port, context._plugin_context,
                                 switch_name, switch_port,
                                 network[provider_net.PHYSICAL_NETWORK],
                                 context,
                                 segmentation_id)
        # Baremetal Operations
        elif self._is_port_bound(context.current):
            port = context.current
            provisioning_blocks.provisioning_complete(
                context._plugin_context, port['id'], resources.PORT,
                c.NETWORKING_ENTITY)

        elif self._is_port_bound(context.original):
            port = context.original
            network = context.network.current
            mappings, segmentation_id = self.get_switch_meta(port, network)

            for switch_name, switch_port in mappings:
                LOG.debug('Ensuring Updated port {switch_port} on network '
                          '{network} {switch_name} to vlan: '
                          '{segmentation_id}'.format(
                              switch_port=switch_port,
                              network=network,
                              switch_name=switch_name,
                              segmentation_id=segmentation_id))

                self.ensure_port(port, context._plugin_context,
                                 switch_name, switch_port,
                                 network[provider_net.PHYSICAL_NETWORK],
                                 context,
                                 segmentation_id)

    def delete_port_postcommit(self, context):
        """Delete a port.

        :param context: PortContext instance describing the current
        state of the port, prior to the call to delete it.

        Called after the transaction completes. Call can block, though
        will block the entire process so care should be taken to not
        drastically affect performance.  Runtime errors are not
        expected, and will not prevent the resource from being
        deleted.
        """
        port = context.current
        network = context.network.current

        if self._is_port_bound(context.current):

            mappings, segmentation_id = self.get_switch_meta(port, network)

            for switch_name, switch_port in mappings:
                LOG.debug('Ensuring Deleted port {switch_port} on '
                          '{switch_name} to vlan: {segmentation_id}'.format(
                              switch_port=switch_port,
                              switch_name=switch_name,
                              segmentation_id=segmentation_id))

                self.ensure_port(port, context._plugin_context,
                                 switch_name, switch_port,
                                 network[provider_net.PHYSICAL_NETWORK],
                                 context,
                                 segmentation_id, delete=True)

    def bind_port(self, context):
        """Attempt to bind a port.

        :param context: PortContext instance describing the port

        This method is called outside any transaction to attempt to
        establish a port binding using this mechanism driver. Bindings
        may be created at each of multiple levels of a hierarchical
        network, and are established from the top level downward. At
        each level, the mechanism driver determines whether it can
        bind to any of the network segments in the
        context.segments_to_bind property, based on the value of the
        context.host property, any relevant port or network
        attributes, and its own knowledge of the network topology. At
        the top level, context.segments_to_bind contains the static
        segments of the port's network. At each lower level of
        binding, it contains static or dynamic segments supplied by
        the driver that bound at the level above. If the driver is
        able to complete the binding of the port to any segment in
        context.segments_to_bind, it must call context.set_binding
        with the binding details. If it can partially bind the port,
        it must call context.continue_binding with the network
        segments to be used to bind at the next lower level.

        If the binding results are committed after bind_port returns,
        they will be seen by all mechanism drivers as
        update_port_precommit and update_port_postcommit calls. But if
        some other thread or process concurrently binds or updates the
        port, these binding results will not be committed, and
        update_port_precommit and update_port_postcommit will not be
        called on the mechanism drivers with these results. Because
        binding results can be discarded rather than committed,
        drivers should avoid making persistent state changes in
        bind_port, or else must ensure that such state changes are
        eventually cleaned up.

        Implementing this method explicitly declares the mechanism
        driver as having the intention to bind ports. This is inspected
        by the QoS service to identify the available QoS rules you
        can use with ports.
        """

        port = context.current
        network = context.network.current

        # Run this before getting switch meta, switch_meta_from_local_info
        # runs the same condition and will raise before this is checked
        if not self._is_port_supported(port):
            LOG.warning('Port {} has vnic_type: {}'
                        ' which is not supported by networking-ansible'
                        ', ignoring.'.format(
                            port['id'],
                            port[portbindings.VNIC_TYPE]))
            return

        mappings, segmentation_id = self.get_switch_meta(port, network)

        for switch_name, switch_port in mappings:

            LOG.debug('Plugging in port {switch_port} on '
                      '{switch_name} to vlan: {segmentation_id}'.format(
                          switch_port=switch_port,
                          switch_name=switch_name,
                          segmentation_id=segmentation_id))

            provisioning_blocks.add_provisioning_component(
                context._plugin_context, port['id'], resources.PORT,
                c.NETWORKING_ENTITY)

            self.ensure_port(port, context._plugin_context,
                             switch_name, switch_port,
                             network[provider_net.PHYSICAL_NETWORK], context,
                             segmentation_id)

    def get_switch_meta(self, port, network=None):
        '''
        port: neutron port object
        network: neutron network object
        returns: list of mappings tuples and segmentation_id
                 mapping tuple: ('switch_name', 'switch_port')
        '''
        if self._is_port_baremetal(port):
            return self._switch_meta_from_link_info(port, network)
        elif self._is_port_normal(port):
            return self._switch_meta_from_port_host_id(port, network)
        return None, None

    def _switch_meta_from_link_info(self, port, network=None):
        network = network or {}

        local_link_info = self._get_port_lli(port)

        if not local_link_info:
            msg = 'local_link_information is missing in port {port_id} ' \
                  'binding:profile'.format(port_id=port['id'])
            LOG.debug(msg)
            raise exceptions.LocalLinkInfoMissingException(msg)
        switch_mac = local_link_info[0].get('switch_id', '').upper()
        switch_name = local_link_info[0].get('switch_info')
        switch_port = local_link_info[0].get('port_id')
        # fill in the switch name if mac exists but name is not defined
        # this provides support for introspection when the switch's mac is
        # also provided in the ML2 conf for ansible-networking
        if not switch_name and switch_mac in self.ml2config.mac_map:
            switch_name = self.ml2config.mac_map[switch_mac]
        segmentation_id = network.get(provider_net.SEGMENTATION_ID, '')
        LOG.debug('Local Link Info:: name: {} mac: {} port: {}'.format(
            switch_name, switch_mac, switch_port))
        return [(switch_name, switch_port)], segmentation_id

    def _switch_meta_from_port_host_id(self, port, network=None):
        network = network or {}
        # TODO What do we do if there is not a mapping available?
        #      should we fail in some way?
        host_id = port[portbindings.HOST_ID]
        # if type direct get the pci address and append to host_id
        if AnsibleMechanismDriver._is_port_direct(port):
            host_id = self._build_sriov_host_id(port, host_id)
        LOG.debug('Host-ID lookup: {}'.format(host_id))
        mappings = self.ml2config.port_mappings.get(host_id, [])
        segmentation_id = network.get(provider_net.SEGMENTATION_ID, '')
        return mappings, segmentation_id

    def ensure_subports(self, port_id, db):
        # set the correct state on port in the case where it has subports.

        port = Port.get_object(db, id=port_id)

        # If the parent port has been deleted then that delete will handle
        # removing the trunked vlans on the switch using the mac
        if not port:
            LOG.debug('Discarding attempt to ensure subports on a port'
                      'that has been deleted')
            return

        # get switch info
        mappings, segmentation_id = self.get_switch_meta(port)

        for switch_name, switch_port in mappings:
            # lock switch
            lock = self.coordinator.get_lock(switch_name)
            with lock:
                # get updated port from db
                updated_port = Port.get_object(db, id=port_id)
                if updated_port:
                    self._set_port_state(updated_port, db,
                                         switch_name, switch_port)
                    return
                else:
                    # port delete operation will take care of deletion
                    LOG.debug('Discarding attempt to ensure subports on a port'
                              ' {} that was deleted after lock '
                              'acquisition'.format(port_id))
                    return

    def ensure_port(self, port, db, switch_name,
                    switch_port, physnet, port_context,
                    segmentation_id, delete=False):
        LOG.debug('Ensuring state of port {port_id} '
                  'with mac addr {mac} '
                  'on switch {switch_name} '
                  'on port {switch_port} '
                  'on physnet {physnet} '
                  'using db context {db}'.format(port_id=port['id'],
                                                 mac=port['mac_address'],
                                                 switch_name=switch_name,
                                                 switch_port=switch_port,
                                                 physnet=physnet,
                                                 db=db))

        if not self.net_runr.has_host(switch_name):
            raise ml2_exc.MechanismDriverError('NetAnsible: couldnt find '
                                               'switch_name {} in network '
                                               'runner inventory while '
                                               'configuring port id '
                                               '{}'.format(switch_name,
                                                           port['id']))

        # get dlock for the switch we're working with
        lock = self.coordinator.get_lock(switch_name)
        with lock:
            # port = get the port from the db
            updated_port = Port.get_object(db, id=port['id'])

            if self._is_port_normal(port):
                # OVS handles the port binding for the VM. There's no awareness
                # that the compute node port is being configured in openstack.
                # By the time we get the port object it's already been handled
                # by OVS so we can't detect if it's being deleted by its state.
                # We have to rely on the hook that's called to indicate
                # whether to do an update or delete. Since ensure port handles
                # both the delete flag needs to be passed for VM ports.
                if delete:
                    # Get active ports on this port's network
                    # We should not delete the vlan from the compute node's
                    # trunk if there are other ports still using the vlan
                    active_ports = Port.get_objects(
                        db,
                        network_id=port['network_id'],
                        device_owner=c.COMPUTE_NOVA)

                    LOG.debug('Active Ports: {}'.format(active_ports))

                    # Get_objects can't filter by binding:host_id so we use a
                    # python filter function to finish filtering the ports by
                    # compute host_id
                    def active_port_filter(db_port):
                        for binding in db_port.bindings:
                            host_id = port[portbindings.HOST_ID]
                            if AnsibleMechanismDriver._is_port_direct(port):
                                host_id = self._build_sriov_host_id(
                                    port, host_id)
                                db_network = Network.get_object(
                                    db, id=db_port['network_id'])
                                mappings, db_segid = self.get_switch_meta(
                                    db_port, db_network)
                                # first mapping, should be only one for DIRECT
                                # port from (switch_name, switch_port) tuple
                                db_switchport = mappings[0][1]
                                if db_switchport == switch_port \
                                    and db_segid == segmentation_id \
                                    and db_port['id'] != port['id']:
                                    return True
                            else:
                                if binding['host'] == host_id \
                                    and db_port['id'] != port['id']:
                                    return True
                        # Default to false
                        return False

                    active_ports = list(filter(active_port_filter,
                                               active_ports))
                    LOG.debug('Filtered Active Ports: {}'.format(active_ports))

                    # If there are other VM's active ports on this port's
                    # network we will skip removing the vlan from the
                    # compute node's trunk port
                    if not active_ports:
                        self.net_runr.delete_trunk_vlan(
                            switch_name,
                            switch_port,
                            segmentation_id,
                            **self.kwargs[switch_name])
                    else:
                        LOG.info('Skip removing Segmentation ID {} from '
                                 'compute host {}. There are {} other '
                                 'active ports using the VLAN.'.format(
                                     segmentation_id,
                                     port[portbindings.HOST_ID],
                                     len(active_ports)))

                else:
                    self._set_port_state(port, db, switch_name, switch_port)

                return

            # if baremetal port exists and is bound to a port
            elif self._get_port_lli(updated_port):

                if self._set_port_state(updated_port, db,
                                        switch_name, switch_port):
                    if port_context and port_context.segments_to_bind:
                        segments = port_context.segments_to_bind
                        port_context.set_binding(segments[0][ml2api.ID],
                                                 portbindings.VIF_TYPE_OTHER,
                                                 {})
                    return

            else:
                # if the port doesn't exist, we have a mac+switch, we can look
                # up whether the port needs to be deleted on the switch
                if self._is_deleted_port_in_use(physnet,
                                                port['mac_address'], db):
                    LOG.debug('Port {port_id} was deleted, but its switch'
                              'port {sp} is now in use by another port, '
                              'discarding request to delete'.format(
                                  port_id=port['id'],
                                  sp=switch_port))
                    return
                else:
                    self._delete_switch_port(switch_name, switch_port)

    def _set_port_state(self, port, db, switch_name, switch_port):
        if not port:
            # error
            raise ml2_exc.MechanismDriverError('Null port passed to '
                                               'set_port_state')

        if not switch_name or not switch_port:
            # error/raise
            raise ml2_exc.MechanismDriverError('NetAnsible: couldnt find '
                                               'switch_name {} or switch '
                                               'port {} to set port '
                                               'state'.format(switch_name,
                                                              switch_port))

        if not self.net_runr.has_host(switch_name):
            raise ml2_exc.MechanismDriverError('NetAnsible: couldnt find '
                                               'switch_name {} in '
                                               'inventory'.format(
                                                   switch_name))

        network = Network.get_object(db, id=port['network_id'])
        if not network:
            raise ml2_exc.MechanismDriverError('NetAnsible: couldnt find '
                                               'network for port '
                                               '{}'.format(port.id))

        trunk = Trunk.get_object(db, port_id=port['id'])

        segmentation_id = network.segments[0].segmentation_id
        # Assign port to network
        try:
            if trunk:
                sub_ports = trunk.sub_ports
                trunked_vlans = [sp.segmentation_id for sp in sub_ports]
                self.net_runr.conf_trunk_port(switch_name,
                                              switch_port,
                                              segmentation_id,
                                              trunked_vlans,
                                              **self.kwargs[switch_name])

            elif self._is_port_normal(port):
                self.net_runr.add_trunk_vlan(switch_name,
                                             switch_port,
                                             segmentation_id,
                                             **self.kwargs[switch_name])

            else:
                self.net_runr.conf_access_port(
                    switch_name,
                    switch_port,
                    segmentation_id,
                    **self.kwargs[switch_name])

            LOG.info('Port {neutron_port} has been plugged into '
                     'switch port {sp} on device {switch_name}'.format(
                         neutron_port=port['id'],
                         sp=switch_port,
                         switch_name=switch_name))
            return True
        except Exception as e:
            LOG.error('Failed to plug port {neutron_port} into '
                      'switch port: {sp} on device: {sw} '
                      'reason: {exc}'.format(
                          neutron_port=port['id'],
                          sp=switch_port,
                          sw=switch_name,
                          exc=e))
            raise exceptions.NetworkingAnsibleMechException(e)

    def _delete_switch_port(self, switch_name, switch_port):
        # we want to delete the physical port on the switch
        # provided since it's no longer in use
        LOG.debug('Unplugging port {switch_port} '
                  'on {switch_name}'.format(switch_port=switch_port,
                                            switch_name=switch_name))
        try:
            self.net_runr.delete_port(switch_name,
                                      switch_port,
                                      **self.kwargs[switch_name])
            LOG.info('Unplugged port {switch_port} '
                     'on {switch_name}'.format(switch_port=switch_port,
                                               switch_name=switch_name))
        except Exception as e:
            LOG.error('Failed to uplug port {switch_port} '
                      'on {switch_name} '
                      'reason: {exc}'.format(
                          switch_port=switch_port,
                          switch_name=switch_name,
                          exc=e))
            raise exceptions.NetworkingAnsibleMechException(e)

    def _is_deleted_port_in_use(self, physnet, mac, db):
        # Go through all ports with this mac addr and find which
        # network segment they are on, which will contain physnet
        # and net type
        #
        # if a port with this mac on the same physical provider
        # is attached to this port then don't delete the interface.
        # Don't need to check the segment since delete will remove all
        # segments, and bind also deletes before adding
        #
        # These checks are required because a second tenant could
        # set their mac address to the ironic port one and attempt
        # meddle with the port delete. This wouldn't do anything
        # bad today because bind deletes the interface and recreates
        # it on the physical switch, but that's an implementation
        # detail we shouldn't rely on.

        # get port by mac
        mports = Port.get_objects(db, mac_address=mac)

        if len(mports) > 1:
            # This isn't technically a problem, but it may indicate something
            # fishy is going on
            LOG.warn('multiple ports matching ironic '
                     'port mac: {mac}'.format(mac=mac))

        for port in mports:
            if port.bindings:
                lli = self._get_port_lli(port)
                if lli:
                    net = Network.get_object(db, id=port.network_id)
                    if net:
                        for seg in net.segments:
                            if seg.physical_network == physnet and \
                               seg.network_type == 'vlan':
                                return True
        return False

    @staticmethod
    def _is_port_supported(port):
        """Return whether a port is supported by this driver.

        :param port: The port to check
        :returns: Whether the port is supported

        """
        vnic_type = port[portbindings.VNIC_TYPE]
        return vnic_type in c.SUPPORTED_TYPES

    @staticmethod
    def _is_port_baremetal(port):
        """Return whether a port is for baremetal server.

        :param port: The port to check
        :returns: Whether the port is for baremetal

        """
        vnic_type = port[portbindings.VNIC_TYPE]
        return vnic_type == portbindings.VNIC_BAREMETAL

    @staticmethod
    def _is_port_normal(port):
        """Return whether a port is for VM server.

        :param port: The port to check
        :returns: Whether the port is for type normal

        """
        device_owner = port[c.DEVICE_OWNER]
        return device_owner == c.COMPUTE_NOVA

    def _is_port_direct(port):
        """Return whether a port is type direct

        :param port: The port to check
        :returns: Whether the port is type direct
        """
        vnic_type = port[portbindings.VNIC_TYPE]
        return vnic_type == portbindings.VNIC_DIRECT

    @staticmethod
    def _is_port_bound(port):
        """Return whether a port is bound by this driver.

        Baremetal ports bound by this driver have a VIF type 'other'.

        :param port: The port to check
        :returns: Whether the port is bound
        """
        if not AnsibleMechanismDriver._is_port_supported(port):
            return False

        vif_type = port[portbindings.VIF_TYPE]
        # Check type Baremetal bound
        if AnsibleMechanismDriver._is_port_baremetal(port):
            return vif_type == portbindings.VIF_TYPE_OTHER

        # Check type OVS bound
        if AnsibleMechanismDriver._is_port_normal(port):
            return vif_type == portbindings.VIF_TYPE_OVS

        # Default: not bound
        return False

    @staticmethod
    def _get_port_lli(port):
        """Return the local link info for a port

        :param port: The port to get link info from
        :returns: The link info or None
        """
        if not port:
            return None
        # Validate port and local link info
        if isinstance(port, Port):
            local_link_info = port.bindings[0].profile.get(c.LLI)
        else:
            if not AnsibleMechanismDriver._is_port_supported(port):
                return None
            local_link_info = port[portbindings.PROFILE].get(c.LLI)
        return local_link_info

    @staticmethod
    def _build_sriov_host_id(port, host_id):
        profile = port.get(portbindings.PROFILE, {})
        pci_slot = profile.get('pci_slot')
        pci_slot = pci_slot.split('.')[0]
        pci_slot = pci_slot.replace('0000:', '')
        pci_slot = pci_slot.replace(':', '')
        return '{}-{}'.format(host_id, pci_slot)
