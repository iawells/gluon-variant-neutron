#    (c) Copyright 2014 Hewlett-Packard Development Company, L.P.
#    All Rights Reserved.
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

import random

from oslo_db import exception as db_exc
from oslo_log import log as logging
import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.orm import joinedload

from neutron._i18n import _LI, _LW
from neutron.callbacks import events
from neutron.callbacks import registry
from neutron.callbacks import resources
from neutron.common import constants as n_const
from neutron.common import utils as n_utils
from neutron.db import agents_db
from neutron.db import l3_agentschedulers_db as l3agent_sch_db
from neutron.db import model_base
from neutron.db import models_v2
from neutron.extensions import l3agentscheduler
from neutron.extensions import portbindings
from neutron import manager
from neutron.plugins.common import constants as service_constants
from neutron.plugins.ml2 import db as ml2_db

LOG = logging.getLogger(__name__)


class CentralizedSnatL3AgentBinding(model_base.BASEV2):
    """Represents binding between Neutron Centralized SNAT and L3 agents."""

    __tablename__ = "csnat_l3_agent_bindings"

    router_id = sa.Column(sa.String(36),
                          sa.ForeignKey("routers.id", ondelete='CASCADE'),
                          primary_key=True)
    l3_agent_id = sa.Column(sa.String(36),
                            sa.ForeignKey("agents.id", ondelete='CASCADE'),
                            primary_key=True)
    host_id = sa.Column(sa.String(255))
    csnat_gw_port_id = sa.Column(sa.String(36),
                                 sa.ForeignKey('ports.id', ondelete='CASCADE'))
    l3_agent = orm.relationship(agents_db.Agent)
    csnat_gw_port = orm.relationship(models_v2.Port)


class L3_DVRsch_db_mixin(l3agent_sch_db.L3AgentSchedulerDbMixin):
    """Mixin class for L3 DVR scheduler.

    DVR currently supports the following use cases:

     - East/West (E/W) traffic between VMs: this is handled in a
       distributed manner across Compute Nodes without a centralized element.
       This includes E/W traffic between VMs on the same Compute Node.
     - North/South traffic for Floating IPs (FIP N/S): this is supported on the
       distributed routers on Compute Nodes without any centralized element.
     - North/South traffic for SNAT (SNAT N/S): this is supported via a
       centralized element that handles the SNAT traffic.

    To support these use cases,  DVR routers rely on an L3 agent that runs on a
    central node (also known as Network Node or Service Node),  as well as, L3
    agents that run individually on each Compute Node of an OpenStack cloud.

    Each L3 agent creates namespaces to route traffic according to the use
    cases outlined above.  The mechanism adopted for creating and managing
    these namespaces is via (Router,  Agent) binding and Scheduling in general.

    The main difference between distributed routers and centralized ones is
    that in the distributed case,  multiple bindings will exist,  one for each
    of the agents participating in the routed topology for the specific router.

    These bindings are created in the following circumstances:

    - A subnet is added to a router via router-interface-add, and that subnet
      has running VM's deployed in it.  A binding will be created between the
      router and any L3 agent whose Compute Node is hosting the VM(s).
    - An external gateway is set to a router via router-gateway-set.  A binding
      will be created between the router and the L3 agent running centrally
      on the Network Node.

    Therefore,  any time a router operation occurs (create, update or delete),
    scheduling will determine whether the router needs to be associated to an
    L3 agent, just like a regular centralized router, with the difference that,
    in the distributed case,  the bindings required are established based on
    the state of the router and the Compute Nodes.
    """

    def dvr_handle_new_service_port(self, context, port):
        """Handle new dvr service port creation.

        When a new dvr service port is created, this function will
        schedule a dvr router to new compute node if needed and notify
        l3 agent on that node.
        """
        port_host = port[portbindings.HOST_ID]
        l3_agent_on_host = (self.get_l3_agents(
            context, filters={'host': [port_host]}) or [None])[0]
        if not l3_agent_on_host:
            return

        ips = port['fixed_ips']
        router_ids = self.get_dvr_routers_by_portid(context, port['id'], ips)
        for router_id in router_ids:
            if not self.check_l3_agent_router_binding(
                    context, router_id, l3_agent_on_host['id']):
                self.schedule_router(
                    context, router_id, candidates=[l3_agent_on_host])
            LOG.debug('DVR: Handle new service_port on router: %s', router_id)

        self.l3_rpc_notifier.routers_updated_on_host(
            context, router_ids, port_host)

    def get_dvr_routers_by_portid(self, context, port_id, fixed_ips=None):
        """Gets the dvr routers on vmport subnets."""
        router_ids = set()
        if fixed_ips is None:
            port_dict = self._core_plugin.get_port(context, port_id)
            fixed_ips = port_dict['fixed_ips']
        for fixedip in fixed_ips:
            vm_subnet = fixedip['subnet_id']
            filter_sub = {'fixed_ips': {'subnet_id': [vm_subnet]},
                          'device_owner':
                          [n_const.DEVICE_OWNER_DVR_INTERFACE]}
            subnet_ports = self._core_plugin.get_ports(
                context, filters=filter_sub)
            for subnet_port in subnet_ports:
                router_ids.add(subnet_port['device_id'])
        return router_ids

    def get_subnet_ids_on_router(self, context, router_id):
        """Return subnet IDs for interfaces attached to the given router."""
        subnet_ids = set()
        filter_rtr = {'device_id': [router_id]}
        int_ports = self._core_plugin.get_ports(context, filters=filter_rtr)
        for int_port in int_ports:
            int_ips = int_port['fixed_ips']
            if int_ips:
                int_subnet = int_ips[0]['subnet_id']
                subnet_ids.add(int_subnet)
            else:
                LOG.debug('DVR: Could not find a subnet id '
                          'for router %s', router_id)
        return subnet_ids

    def dvr_deletens_if_no_port(self, context, port_id, port_host=None):
        """Delete the DVR namespace if no dvr serviced port exists."""
        admin_context = context.elevated()
        router_ids = self.get_dvr_routers_by_portid(admin_context, port_id)
        if not port_host:
            port_host = ml2_db.get_port_binding_host(admin_context.session,
                                                     port_id)
            if not port_host:
                LOG.debug('Host name not found for port %s', port_id)
                return []

        if not router_ids:
            LOG.debug('No namespaces available for this DVR port %(port)s '
                      'on host %(host)s', {'port': port_id,
                                           'host': port_host})
            return []
        removed_router_info = []
        for router_id in router_ids:
            subnet_ids = self.get_subnet_ids_on_router(admin_context,
                                                       router_id)
            if self.check_dvr_serviceable_ports_on_host(
                    admin_context, port_host, subnet_ids, except_port=port_id):
                continue
            filter_rtr = {'device_id': [router_id],
                          'device_owner':
                          [n_const.DEVICE_OWNER_DVR_INTERFACE]}
            int_ports = self._core_plugin.get_ports(
                admin_context, filters=filter_rtr)
            for port in int_ports:
                dvr_binding = (ml2_db.
                               get_dvr_port_binding_by_host(context.session,
                                                            port['id'],
                                                            port_host))
                if dvr_binding:
                    # unbind this port from router
                    dvr_binding['router_id'] = None
                    dvr_binding.update(dvr_binding)
            agent = self._get_agent_by_type_and_host(context,
                                                     n_const.AGENT_TYPE_L3,
                                                     port_host)
            info = {'router_id': router_id, 'host': port_host,
                    'agent_id': str(agent.id)}
            removed_router_info.append(info)
            LOG.debug('Router namespace %(router_id)s on host %(host)s '
                      'to be deleted', info)
        return removed_router_info

    def bind_snat_router(self, context, router_id, chosen_agent):
        """Bind the router to the chosen l3 agent."""
        with context.session.begin(subtransactions=True):
            binding = CentralizedSnatL3AgentBinding()
            binding.l3_agent = chosen_agent
            binding.router_id = router_id
            context.session.add(binding)
            LOG.debug('SNAT Router %(router_id)s is scheduled to L3 agent '
                      '%(agent_id)s', {'router_id': router_id,
                                       'agent_id': chosen_agent.id})

    def bind_dvr_router_servicenode(self, context, router_id,
                                    chosen_snat_agent):
        """Bind the IR router to service node if not already hosted."""
        query = (context.session.query(l3agent_sch_db.RouterL3AgentBinding).
                 filter_by(router_id=router_id))
        for bind in query:
            if bind.l3_agent_id == chosen_snat_agent.id:
                LOG.debug('Distributed Router %(router_id)s already hosted '
                          'on snat l3_agent %(snat_id)s',
                          {'router_id': router_id,
                           'snat_id': chosen_snat_agent.id})
                return
        with context.session.begin(subtransactions=True):
            binding = l3agent_sch_db.RouterL3AgentBinding()
            binding.l3_agent = chosen_snat_agent
            binding.router_id = router_id
            context.session.add(binding)
            LOG.debug('Binding the distributed router %(router_id)s to '
                      'the snat agent %(snat_id)s',
                      {'router_id': router_id,
                       'snat_id': chosen_snat_agent.id})

    def bind_snat_servicenode(self, context, router_id, snat_candidates):
        """Bind the snat router to the chosen l3 service agent."""
        chosen_snat_agent = random.choice(snat_candidates)
        self.bind_snat_router(context, router_id, chosen_snat_agent)
        return chosen_snat_agent

    def unbind_snat(self, context, router_id, agent_id=None):
        """Unbind snat from the chosen l3 service agent.

        Unbinds from all L3 agents hosting SNAT if passed agent_id is None
        """
        with context.session.begin(subtransactions=True):
            query = (context.session.
                     query(CentralizedSnatL3AgentBinding).
                     filter_by(router_id=router_id))
            if agent_id:
                query = query.filter_by(l3_agent_id=agent_id)
            binding = query.first()
            if not binding:
                LOG.debug('no SNAT router binding found for router: '
                          '%(router)s, agent: %(agent)s',
                          {'router': router_id, 'agent': agent_id or 'any'})
                return

            query.delete()
        LOG.debug('Deleted binding of the SNAT router %s', router_id)

        return binding

    def unbind_router_servicenode(self, context, router_id, binding):
        """Unbind the router from the chosen l3 service agent."""
        port_found = False
        with context.session.begin(subtransactions=True):
            host = binding.l3_agent.host
            subnet_ids = self.get_subnet_ids_on_router(context, router_id)
            for subnet in subnet_ids:
                ports = (
                    self._core_plugin.get_ports_on_host_by_subnet(
                        context, host, subnet))
                for port in ports:
                    if (n_utils.is_dvr_serviced(port['device_owner'])):
                        port_found = True
                        LOG.debug('One or more ports exist on the snat '
                                  'enabled l3_agent host %(host)s and '
                                  'router_id %(id)s',
                                  {'host': host, 'id': router_id})
                        break
            agent_id = binding.l3_agent_id

            if not port_found:
                context.session.query(
                    l3agent_sch_db.RouterL3AgentBinding).filter_by(
                        router_id=router_id, l3_agent_id=agent_id).delete(
                            synchronize_session=False)

        if not port_found:
            self.l3_rpc_notifier.router_removed_from_agent(
                context, router_id, host)
            LOG.debug('Removed binding for router %(router_id)s and '
                      'agent %(agent_id)s',
                      {'router_id': router_id, 'agent_id': agent_id})
        return port_found

    def unbind_snat_servicenode(self, context, router_id):
        """Unbind snat AND the router from the current agent."""
        with context.session.begin(subtransactions=True):
            binding = self.unbind_snat(context, router_id)
            if binding:
                self.unbind_router_servicenode(context, router_id, binding)

    def get_snat_bindings(self, context, router_ids):
        """Retrieves the dvr snat bindings for a router."""
        if not router_ids:
            return []
        query = context.session.query(CentralizedSnatL3AgentBinding)
        query = query.options(joinedload('l3_agent')).filter(
            CentralizedSnatL3AgentBinding.router_id.in_(router_ids))
        return query.all()

    def get_snat_candidates(self, sync_router, l3_agents):
        """Get the valid snat enabled l3 agents for the distributed router."""
        candidates = []
        is_router_distributed = sync_router.get('distributed', False)
        if not is_router_distributed:
            return candidates
        for l3_agent in l3_agents:
            if not l3_agent.admin_state_up:
                continue

            agent_conf = self.get_configuration_dict(l3_agent)
            agent_mode = agent_conf.get(n_const.L3_AGENT_MODE,
                                        n_const.L3_AGENT_MODE_LEGACY)
            if agent_mode != n_const.L3_AGENT_MODE_DVR_SNAT:
                continue

            router_id = agent_conf.get('router_id', None)
            if router_id and router_id != sync_router['id']:
                continue

            handle_internal_only_routers = agent_conf.get(
                'handle_internal_only_routers', True)
            gateway_external_network_id = agent_conf.get(
                'gateway_external_network_id', None)
            ex_net_id = (sync_router['external_gateway_info'] or {}).get(
                'network_id')
            if ((not ex_net_id and not handle_internal_only_routers) or
                (ex_net_id and gateway_external_network_id and
                 ex_net_id != gateway_external_network_id)):
                continue

            candidates.append(l3_agent)
        return candidates

    def schedule_snat_router(self, context, router_id, sync_router):
        """Schedule the snat router on l3 service agent."""
        active_l3_agents = self.get_l3_agents(context, active=True)
        if not active_l3_agents:
            LOG.warn(_LW('No active L3 agents found for SNAT'))
            return
        snat_candidates = self.get_snat_candidates(sync_router,
                                                   active_l3_agents)
        if not snat_candidates:
            LOG.warn(_LW('No candidates found for SNAT'))
            return
        else:
            try:
                chosen_agent = self.bind_snat_servicenode(
                    context, router_id, snat_candidates)
            except db_exc.DBDuplicateEntry:
                LOG.info(_LI("SNAT already bound to a service node."))
                return
            self.bind_dvr_router_servicenode(
                context, router_id, chosen_agent)
            return chosen_agent

    def _unschedule_router(self, context, router_id, agents_ids):
        router = self.get_router(context, router_id)
        if router.get('distributed', False):
            # for DVR router unscheduling means just unscheduling SNAT portion
            self.unbind_snat_servicenode(context, router_id)
        else:
            super(L3_DVRsch_db_mixin, self)._unschedule_router(
                context, router_id, agents_ids)

    def _get_active_l3_agent_routers_sync_data(self, context, host, agent,
                                               router_ids):
        if n_utils.is_extension_supported(self, n_const.L3_HA_MODE_EXT_ALIAS):
            return self.get_ha_sync_data_for_host(context, host,
                                                  router_ids=router_ids,
                                                  active=True)
        return self._get_dvr_sync_data(context, host, agent,
                                       router_ids=router_ids, active=True)

    def check_agent_router_scheduling_needed(self, context, agent, router):
        if router.get('distributed'):
            if router['external_gateway_info']:
                return not self.get_snat_bindings(context, [router['id']])
            return False
        return super(L3_DVRsch_db_mixin,
                     self).check_agent_router_scheduling_needed(
                     context, agent, router)

    def create_router_to_agent_binding(self, context, agent, router):
        """Create router to agent binding."""
        router_id = router['id']
        agent_id = agent['id']
        if router['external_gateway_info'] and self.router_scheduler and (
                router.get('distributed')):
            try:
                self.bind_snat_router(context, router_id, agent)
                self.bind_dvr_router_servicenode(context,
                                                 router_id, agent)
            except db_exc.DBError:
                raise l3agentscheduler.RouterSchedulingFailed(
                    router_id=router_id,
                    agent_id=agent_id)
        else:
            super(L3_DVRsch_db_mixin, self).create_router_to_agent_binding(
                  context, agent, router)

    def remove_router_from_l3_agent(self, context, agent_id, router_id):
        binding = None
        router = self.get_router(context, router_id)
        if router['external_gateway_info'] and router.get('distributed'):
            binding = self.unbind_snat(context, router_id, agent_id=agent_id)
            # binding only exists when agent mode is dvr_snat
            if binding:
                notification_not_sent = self.unbind_router_servicenode(context,
                                             router_id, binding)
                if notification_not_sent:
                    self.l3_rpc_notifier.routers_updated(
                        context, [router_id], schedule_routers=False)

        # Below Needs to be done when agent mode is legacy or dvr.
        if not binding:
            super(L3_DVRsch_db_mixin,
                  self).remove_router_from_l3_agent(
                    context, agent_id, router_id)


def _notify_l3_agent_new_port(resource, event, trigger, **kwargs):
    LOG.debug('Received %(resource)s %(event)s', {
        'resource': resource,
        'event': event})
    port = kwargs.get('port')
    if not port:
        return

    if n_utils.is_dvr_serviced(port['device_owner']):
        l3plugin = manager.NeutronManager.get_service_plugins().get(
            service_constants.L3_ROUTER_NAT)
        context = kwargs['context']
        l3plugin.dvr_handle_new_service_port(context, port)
        l3plugin.update_arp_entry_for_dvr_service_port(context, port, "add")


def _notify_port_delete(event, resource, trigger, **kwargs):
    context = kwargs['context']
    port = kwargs['port']
    removed_routers = kwargs['removed_routers']
    l3plugin = manager.NeutronManager.get_service_plugins().get(
        service_constants.L3_ROUTER_NAT)
    l3plugin.update_arp_entry_for_dvr_service_port(context, port, "del")
    for router in removed_routers:
        # we need admin context in case a tenant removes the last dvr
        # serviceable port on a shared network owned by admin, where router
        # is also owned by admin
        l3plugin.remove_router_from_l3_agent(
            context.elevated(), router['agent_id'], router['router_id'])


def _notify_l3_agent_port_update(resource, event, trigger, **kwargs):
    new_port = kwargs.get('port')
    original_port = kwargs.get('original_port')

    if new_port and original_port:
        original_device_owner = original_port.get('device_owner', '')
        new_device_owner = new_port.get('device_owner', '')
        l3plugin = manager.NeutronManager.get_service_plugins().get(
                service_constants.L3_ROUTER_NAT)
        context = kwargs['context']
        is_port_no_longer_serviced = (
            n_utils.is_dvr_serviced(original_device_owner) and
            not n_utils.is_dvr_serviced(new_device_owner))
        is_port_moved = (
            original_port[portbindings.HOST_ID] and
            original_port[portbindings.HOST_ID] !=
            new_port[portbindings.HOST_ID])
        if is_port_no_longer_serviced or is_port_moved:
            removed_routers = l3plugin.dvr_deletens_if_no_port(
                context,
                original_port['id'],
                port_host=original_port[portbindings.HOST_ID])
            if removed_routers:
                removed_router_args = {
                    'context': context,
                    'port': original_port,
                    'removed_routers': removed_routers,
                }
                _notify_port_delete(
                    event, resource, trigger, **removed_router_args)
            if not n_utils.is_dvr_serviced(new_device_owner):
                return
        is_new_port_binding_changed = (
            new_port[portbindings.HOST_ID] and
            (original_port[portbindings.HOST_ID] !=
                new_port[portbindings.HOST_ID]))
        if (is_new_port_binding_changed and
            n_utils.is_dvr_serviced(new_device_owner)):
            l3plugin.dvr_handle_new_service_port(context, new_port)
            l3plugin.update_arp_entry_for_dvr_service_port(
                context, new_port, "add")
        elif kwargs.get('mac_address_updated'):
            l3plugin.update_arp_entry_for_dvr_service_port(
                context, new_port, "add")


def subscribe():
    registry.subscribe(
        _notify_l3_agent_port_update, resources.PORT, events.AFTER_UPDATE)
    registry.subscribe(
        _notify_l3_agent_new_port, resources.PORT, events.AFTER_CREATE)
    registry.subscribe(
        _notify_port_delete, resources.PORT, events.AFTER_DELETE)
