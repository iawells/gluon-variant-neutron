# Copyright (c) 2014 Red Hat, Inc.
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

import functools

import mock
import netaddr

from neutron.agent.l3 import agent as neutron_l3_agent
from neutron.agent.l3 import dvr_fip_ns
from neutron.agent.l3 import dvr_snat_ns
from neutron.agent.l3 import namespaces
from neutron.agent.linux import ip_lib
from neutron.agent.linux import utils
from neutron.common import constants as l3_constants
from neutron.extensions import portbindings
from neutron.tests.common import l3_test_common
from neutron.tests.common import net_helpers
from neutron.tests.functional.agent.l3 import framework


DEVICE_OWNER_COMPUTE = l3_constants.DEVICE_OWNER_COMPUTE_PREFIX + 'fake'


class TestDvrRouter(framework.L3AgentTestFramework):
    def manage_router(self, agent, router):
        def _safe_fipnamespace_delete_on_ext_net(ext_net_id):
            try:
                agent.fipnamespace_delete_on_ext_net(None, ext_net_id)
            except RuntimeError:
                pass
        self.addCleanup(
            _safe_fipnamespace_delete_on_ext_net,
            router['gw_port']['network_id'])

        return super(TestDvrRouter, self).manage_router(agent, router)

    def test_dvr_router_lifecycle_without_ha_without_snat_with_fips(self):
        self._dvr_router_lifecycle(enable_ha=False, enable_snat=False)

    def test_dvr_router_lifecycle_without_ha_with_snat_with_fips(self):
        self._dvr_router_lifecycle(enable_ha=False, enable_snat=True)

    def test_dvr_router_lifecycle_ha_with_snat_with_fips(self):
        self._dvr_router_lifecycle(enable_ha=True, enable_snat=True)

    def _helper_create_dvr_router_fips_for_ext_network(
            self, agent_mode, **dvr_router_kwargs):
        self.agent.conf.agent_mode = agent_mode
        router_info = self.generate_dvr_router_info(**dvr_router_kwargs)
        self.mock_plugin_api.get_external_network_id.return_value = (
            router_info['_floatingips'][0]['floating_network_id'])
        router = self.manage_router(self.agent, router_info)
        fip_ns = router.fip_ns.get_name()
        return router, fip_ns

    def _validate_fips_for_external_network(self, router, fip_ns):
        self.assertTrue(self._namespace_exists(router.ns_name))
        self.assertTrue(self._namespace_exists(fip_ns))
        self._assert_dvr_floating_ips(router)
        self._assert_snat_namespace_does_not_exist(router)

    def test_dvr_router_fips_for_multiple_ext_networks(self):
        agent_mode = 'dvr'
        # Create the first router fip with external net1
        dvr_router1_kwargs = {'ip_address': '19.4.4.3',
                              'subnet_cidr': '19.4.4.0/24',
                              'gateway_ip': '19.4.4.1',
                              'gateway_mac': 'ca:fe:de:ab:cd:ef'}
        router1, fip1_ns = (
            self._helper_create_dvr_router_fips_for_ext_network(
                agent_mode, **dvr_router1_kwargs))
        # Validate the fip with external net1
        self._validate_fips_for_external_network(router1, fip1_ns)

        # Create the second router fip with external net2
        dvr_router2_kwargs = {'ip_address': '19.4.5.3',
                              'subnet_cidr': '19.4.5.0/24',
                              'gateway_ip': '19.4.5.1',
                              'gateway_mac': 'ca:fe:de:ab:cd:fe'}
        router2, fip2_ns = (
            self._helper_create_dvr_router_fips_for_ext_network(
                agent_mode, **dvr_router2_kwargs))
        # Validate the fip with external net2
        self._validate_fips_for_external_network(router2, fip2_ns)

    def _dvr_router_lifecycle(self, enable_ha=False, enable_snat=False,
                              custom_mtu=2000,
                              ip_version=4,
                              dual_stack=False):
        '''Test dvr router lifecycle

        :param enable_ha: sets the ha value for the router.
        :param enable_snat:  the value of enable_snat is used
        to  set the  agent_mode.
        '''

        # The value of agent_mode can be dvr, dvr_snat, or legacy.
        # Since by definition this is a dvr (distributed = true)
        # only dvr and dvr_snat are applicable
        self.agent.conf.agent_mode = 'dvr_snat' if enable_snat else 'dvr'
        self.agent.conf.network_device_mtu = custom_mtu

        # We get the router info particular to a dvr router
        router_info = self.generate_dvr_router_info(
            enable_ha, enable_snat, extra_routes=True)

        # We need to mock the get_agent_gateway_port return value
        # because the whole L3PluginApi is mocked and we need the port
        # gateway_port information before the l3_agent will create it.
        # The port returned needs to have the same information as
        # router_info['gw_port']
        self.mock_plugin_api.get_agent_gateway_port.return_value = router_info[
            'gw_port']

        # We also need to mock the get_external_network_id method to
        # get the correct fip namespace.
        self.mock_plugin_api.get_external_network_id.return_value = (
            router_info['_floatingips'][0]['floating_network_id'])

        # With all that set we can now ask the l3_agent to
        # manage the router (create it, create namespaces,
        # attach interfaces, etc...)
        router = self.manage_router(self.agent, router_info)
        if enable_ha:
            port = router.get_ex_gw_port()
            interface_name = router.get_external_device_name(port['id'])
            self._assert_no_ip_addresses_on_interface(router.ha_namespace,
                                                      interface_name)
            utils.wait_until_true(lambda: router.ha_state == 'master')

            # Keepalived notifies of a state transition when it starts,
            # not when it ends. Thus, we have to wait until keepalived finishes
            # configuring everything. We verify this by waiting until the last
            # device has an IP address.
            device = router.router[l3_constants.INTERFACE_KEY][-1]
            device_exists = functools.partial(
                self.device_exists_with_ips_and_mac,
                device,
                router.get_internal_device_name,
                router.ns_name)
            utils.wait_until_true(device_exists)

        ext_gateway_port = router_info['gw_port']
        self.assertTrue(self._namespace_exists(router.ns_name))
        utils.wait_until_true(
            lambda: self._metadata_proxy_exists(self.agent.conf, router))
        self._assert_internal_devices(router)
        self._assert_dvr_external_device(router)
        self._assert_dvr_gateway(router)
        self._assert_dvr_floating_ips(router)
        self._assert_snat_chains(router)
        self._assert_floating_ip_chains(router)
        self._assert_metadata_chains(router)
        self._assert_rfp_fpr_mtu(router, custom_mtu)
        if enable_snat:
            ip_versions = [4, 6] if (ip_version == 6 or dual_stack) else [4]
            snat_ns_name = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
                router.router_id)
            self._assert_onlink_subnet_routes(
                router, ip_versions, snat_ns_name)
            self._assert_extra_routes(router, namespace=snat_ns_name)
        self._delete_router(self.agent, router.router_id)
        self._assert_fip_namespace_deleted(ext_gateway_port)
        self._assert_router_does_not_exist(router)
        self._assert_snat_namespace_does_not_exist(router)

    def generate_dvr_router_info(self,
                                 enable_ha=False,
                                 enable_snat=False,
                                 agent=None,
                                 extra_routes=False,
                                 **kwargs):
        if not agent:
            agent = self.agent
        router = l3_test_common.prepare_router_data(
            enable_snat=enable_snat,
            enable_floating_ip=True,
            enable_ha=enable_ha,
            extra_routes=extra_routes,
            num_internal_ports=2,
            **kwargs)
        internal_ports = router.get(l3_constants.INTERFACE_KEY, [])
        router['distributed'] = True
        router['gw_port_host'] = agent.conf.host
        router['gw_port'][portbindings.HOST_ID] = agent.conf.host
        floating_ip = router['_floatingips'][0]
        floating_ip['floating_network_id'] = router['gw_port']['network_id']
        floating_ip['host'] = agent.conf.host
        floating_ip['port_id'] = internal_ports[0]['id']
        floating_ip['status'] = 'ACTIVE'

        self._add_snat_port_info_to_router(router, internal_ports)
        # FIP has a dependency on external gateway. So we need to create
        # the snat_port info and fip_agent_gw_port_info irrespective of
        # the agent type the dvr supports. The namespace creation is
        # dependent on the agent_type.
        external_gw_port = router['gw_port']
        self._add_fip_agent_gw_port_info_to_router(router, external_gw_port)
        return router

    def _add_fip_agent_gw_port_info_to_router(self, router, external_gw_port):
        # Add fip agent gateway port information to the router_info
        fip_gw_port_list = router.get(
            l3_constants.FLOATINGIP_AGENT_INTF_KEY, [])
        if not fip_gw_port_list and external_gw_port:
            # Get values from external gateway port
            fixed_ip = external_gw_port['fixed_ips'][0]
            float_subnet = external_gw_port['subnets'][0]
            port_ip = fixed_ip['ip_address']
            # Pick an ip address which is not the same as port_ip
            fip_gw_port_ip = str(netaddr.IPAddress(port_ip) + 5)
            # Add floatingip agent gateway port info to router
            prefixlen = netaddr.IPNetwork(float_subnet['cidr']).prefixlen
            router[l3_constants.FLOATINGIP_AGENT_INTF_KEY] = [
                {'subnets': [
                    {'cidr': float_subnet['cidr'],
                     'gateway_ip': float_subnet['gateway_ip'],
                     'id': fixed_ip['subnet_id']}],
                 'network_id': external_gw_port['network_id'],
                 'device_owner': l3_constants.DEVICE_OWNER_AGENT_GW,
                 'mac_address': 'fa:16:3e:80:8d:89',
                 portbindings.HOST_ID: self.agent.conf.host,
                 'fixed_ips': [{'subnet_id': fixed_ip['subnet_id'],
                                'ip_address': fip_gw_port_ip,
                                'prefixlen': prefixlen}],
                 'id': framework._uuid(),
                 'device_id': framework._uuid()}
            ]

    def _add_snat_port_info_to_router(self, router, internal_ports):
        # Add snat port information to the router
        snat_port_list = router.get(l3_constants.SNAT_ROUTER_INTF_KEY, [])
        if not snat_port_list and internal_ports:
            # Get values from internal port
            port = internal_ports[0]
            fixed_ip = port['fixed_ips'][0]
            snat_subnet = port['subnets'][0]
            port_ip = fixed_ip['ip_address']
            # Pick an ip address which is not the same as port_ip
            snat_ip = str(netaddr.IPAddress(port_ip) + 5)
            # Add the info to router as the first snat port
            # in the list of snat ports
            prefixlen = netaddr.IPNetwork(snat_subnet['cidr']).prefixlen
            router[l3_constants.SNAT_ROUTER_INTF_KEY] = [
                {'subnets': [
                    {'cidr': snat_subnet['cidr'],
                     'gateway_ip': snat_subnet['gateway_ip'],
                     'id': fixed_ip['subnet_id']}],
                 'network_id': port['network_id'],
                 'device_owner': l3_constants.DEVICE_OWNER_ROUTER_SNAT,
                 'mac_address': 'fa:16:3e:80:8d:89',
                 'fixed_ips': [{'subnet_id': fixed_ip['subnet_id'],
                                'ip_address': snat_ip,
                                'prefixlen': prefixlen}],
                 'id': framework._uuid(),
                 'device_id': framework._uuid()}
            ]

    def _assert_dvr_external_device(self, router):
        external_port = router.get_ex_gw_port()
        snat_ns_name = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router.router_id)

        # if the agent is in dvr_snat mode, then we have to check
        # that the correct ports and ip addresses exist in the
        # snat_ns_name namespace
        if self.agent.conf.agent_mode == 'dvr_snat':
            device_exists = functools.partial(
                self.device_exists_with_ips_and_mac,
                external_port,
                router.get_external_device_name,
                snat_ns_name)
            utils.wait_until_true(device_exists)
        # if the agent is in dvr mode then the snat_ns_name namespace
        # should not be present at all:
        elif self.agent.conf.agent_mode == 'dvr':
            self.assertFalse(
                self._namespace_exists(snat_ns_name),
                "namespace %s was found but agent is in dvr mode not dvr_snat"
                % (str(snat_ns_name))
            )
        # if the agent is anything else the test is misconfigured
        # we force a test failure with message
        else:
            self.assertTrue(False, " agent not configured for dvr or dvr_snat")

    def _assert_dvr_gateway(self, router):
        gateway_expected_in_snat_namespace = (
            self.agent.conf.agent_mode == 'dvr_snat'
        )
        if gateway_expected_in_snat_namespace:
            self._assert_dvr_snat_gateway(router)
            self._assert_removal_of_already_deleted_gateway_device(router)

        snat_namespace_should_not_exist = (
            self.agent.conf.agent_mode == 'dvr'
        )
        if snat_namespace_should_not_exist:
            self._assert_snat_namespace_does_not_exist(router)

    def _assert_dvr_snat_gateway(self, router):
        namespace = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router.router_id)
        external_port = router.get_ex_gw_port()
        external_device_name = router.get_external_device_name(
            external_port['id'])
        external_device = ip_lib.IPDevice(external_device_name,
                                          namespace=namespace)
        existing_gateway = (
            external_device.route.get_gateway().get('gateway'))
        expected_gateway = external_port['subnets'][0]['gateway_ip']
        self.assertEqual(expected_gateway, existing_gateway)

    def _assert_removal_of_already_deleted_gateway_device(self, router):
        namespace = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router.router_id)
        device = ip_lib.IPDevice("fakedevice",
                                 namespace=namespace)

        # Assert that no exception is thrown for this case
        self.assertIsNone(router._delete_gateway_device_if_exists(
                          device, "192.168.0.1", 0))

    def _assert_snat_namespace_does_not_exist(self, router):
        namespace = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router.router_id)
        self.assertFalse(self._namespace_exists(namespace))

    def _assert_dvr_floating_ips(self, router):
        # in the fip namespace:
        # Check that the fg-<port-id> (floatingip_agent_gateway)
        # is created with the ip address of the external gateway port
        floating_ips = router.router[l3_constants.FLOATINGIP_KEY]
        self.assertTrue(floating_ips)
        # We need to fetch the floatingip agent gateway port info
        # from the router_info
        floating_agent_gw_port = (
            router.router[l3_constants.FLOATINGIP_AGENT_INTF_KEY])
        self.assertTrue(floating_agent_gw_port)

        external_gw_port = floating_agent_gw_port[0]
        fip_ns = self.agent.get_fip_ns(floating_ips[0]['floating_network_id'])
        fip_ns_name = fip_ns.get_name()
        fg_port_created_successfully = ip_lib.device_exists_with_ips_and_mac(
            fip_ns.get_ext_device_name(external_gw_port['id']),
            [self._port_first_ip_cidr(external_gw_port)],
            external_gw_port['mac_address'],
            namespace=fip_ns_name)
        self.assertTrue(fg_port_created_successfully)
        # Check fpr-router device has been created
        device_name = fip_ns.get_int_device_name(router.router_id)
        fpr_router_device_created_successfully = ip_lib.device_exists(
            device_name, namespace=fip_ns_name)
        self.assertTrue(fpr_router_device_created_successfully)

        # In the router namespace
        # Check rfp-<router-id> is created correctly
        for fip in floating_ips:
            device_name = fip_ns.get_rtr_ext_device_name(router.router_id)
            self.assertTrue(ip_lib.device_exists(
                device_name, namespace=router.ns_name))

    def test_dvr_router_rem_fips_on_restarted_agent(self):
        self.agent.conf.agent_mode = 'dvr_snat'
        router_info = self.generate_dvr_router_info()
        router1 = self.manage_router(self.agent, router_info)
        fip_ns = router1.fip_ns.get_name()
        self.assertTrue(self._namespace_exists(fip_ns))
        restarted_agent = neutron_l3_agent.L3NATAgentWithStateReport(
            self.agent.host, self.agent.conf)
        router1.router[l3_constants.FLOATINGIP_KEY] = []
        self.manage_router(restarted_agent, router1.router)
        self._assert_dvr_snat_gateway(router1)
        self.assertTrue(self._namespace_exists(fip_ns))

    def test_dvr_router_add_fips_on_restarted_agent(self):
        self.agent.conf.agent_mode = 'dvr'
        router_info = self.generate_dvr_router_info()
        router = self.manage_router(self.agent, router_info)
        floating_ips = router.router[l3_constants.FLOATINGIP_KEY]
        router_ns = router.ns_name
        fip_rule_prio_1 = self._get_fixed_ip_rule_priority(
            router_ns, floating_ips[0]['fixed_ip_address'])
        restarted_agent = neutron_l3_agent.L3NATAgent(
            self.agent.host, self.agent.conf)
        floating_ips[0]['floating_ip_address'] = '21.4.4.2'
        floating_ips[0]['fixed_ip_address'] = '10.0.0.2'
        self.manage_router(restarted_agent, router_info)
        fip_rule_prio_2 = self._get_fixed_ip_rule_priority(
            router_ns, floating_ips[0]['fixed_ip_address'])
        self.assertNotEqual(fip_rule_prio_1, fip_rule_prio_2)

    def _get_fixed_ip_rule_priority(self, namespace, fip):
        iprule = ip_lib.IPRule(namespace)
        lines = iprule.rule._as_root([4], ['show']).splitlines()
        for line in lines:
            if fip in line:
                info = iprule.rule._parse_line(4, line)
                return info['priority']

    def test_dvr_router_add_internal_network_set_arp_cache(self):
        # Check that, when the router is set up and there are
        # existing ports on the uplinked subnet, the ARP
        # cache is properly populated.
        self.agent.conf.agent_mode = 'dvr_snat'
        router_info = l3_test_common.prepare_router_data()
        router_info['distributed'] = True
        expected_neighbor = '35.4.1.10'
        port_data = {
            'fixed_ips': [{'ip_address': expected_neighbor}],
            'mac_address': 'fa:3e:aa:bb:cc:dd',
            'device_owner': DEVICE_OWNER_COMPUTE
        }
        self.agent.plugin_rpc.get_ports_by_subnet.return_value = [port_data]
        router1 = self.manage_router(self.agent, router_info)
        internal_device = router1.get_internal_device_name(
            router_info['_interfaces'][0]['id'])
        neighbors = ip_lib.IPDevice(internal_device, router1.ns_name).neigh
        self.assertEqual(expected_neighbor,
                         neighbors.show(ip_version=4).split()[0])

    def _assert_rfp_fpr_mtu(self, router, expected_mtu=1500):
        dev_mtu = self.get_device_mtu(
            router.router_id, router.fip_ns.get_rtr_ext_device_name,
            router.ns_name)
        self.assertEqual(expected_mtu, dev_mtu)
        dev_mtu = self.get_device_mtu(
            router.router_id, router.fip_ns.get_int_device_name,
            router.fip_ns.get_name())
        self.assertEqual(expected_mtu, dev_mtu)

    def test_dvr_router_fip_agent_mismatch(self):
        """Test to validate the floatingip agent mismatch.

        This test validates the condition where floatingip agent
        gateway port host mismatches with the agent and so the
        binding will not be there.

        """
        self.agent.conf.agent_mode = 'dvr'
        router_info = self.generate_dvr_router_info()
        floating_ip = router_info['_floatingips'][0]
        floating_ip['host'] = 'my_new_host'
        # In this case the floatingip binding is different and so it
        # should not create the floatingip namespace on the given agent.
        # This is also like there is no current binding.
        router1 = self.manage_router(self.agent, router_info)
        fip_ns = router1.fip_ns.get_name()
        self.assertTrue(self._namespace_exists(router1.ns_name))
        self.assertFalse(self._namespace_exists(fip_ns))
        self._assert_snat_namespace_does_not_exist(router1)

    def test_dvr_router_fip_late_binding(self):
        """Test to validate the floatingip migration or latebinding.

        This test validates the condition where floatingip private
        port changes while migration or when the private port host
        binding is done later after floatingip association.

        """
        self.agent.conf.agent_mode = 'dvr'
        router_info = self.generate_dvr_router_info()
        fip_agent_gw_port = router_info[l3_constants.FLOATINGIP_AGENT_INTF_KEY]
        # Now let us not pass the FLOATINGIP_AGENT_INTF_KEY, to emulate
        # that the server did not create the port, since there was no valid
        # host binding.
        router_info[l3_constants.FLOATINGIP_AGENT_INTF_KEY] = []
        self.mock_plugin_api.get_agent_gateway_port.return_value = (
            fip_agent_gw_port[0])
        router1 = self.manage_router(self.agent, router_info)
        fip_ns = router1.fip_ns.get_name()
        self.assertTrue(self._namespace_exists(router1.ns_name))
        self.assertTrue(self._namespace_exists(fip_ns))
        self._assert_snat_namespace_does_not_exist(router1)

    def _assert_snat_namespace_exists(self, router):
        namespace = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router.router_id)
        self.assertTrue(self._namespace_exists(namespace))

    def _get_dvr_snat_namespace_device_status(
        self, router, internal_dev_name=None):
        """Function returns the internal and external device status."""
        snat_ns = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router.router_id)
        external_port = router.get_ex_gw_port()
        external_device_name = router.get_external_device_name(
            external_port['id'])
        qg_device_created_successfully = ip_lib.device_exists(
            external_device_name, namespace=snat_ns)
        sg_device_created_successfully = ip_lib.device_exists(
            internal_dev_name, namespace=snat_ns)
        return qg_device_created_successfully, sg_device_created_successfully

    def test_dvr_router_snat_namespace_with_interface_remove(self):
        """Test to validate the snat namespace with interface remove.

        This test validates the snat namespace for all the external
        and internal devices. It also validates if the internal
        device corresponding to the router interface is removed
        when the router interface is deleted.
        """
        self.agent.conf.agent_mode = 'dvr_snat'
        router_info = self.generate_dvr_router_info()
        snat_internal_port = router_info[l3_constants.SNAT_ROUTER_INTF_KEY]
        router1 = self.manage_router(self.agent, router_info)
        csnat_internal_port = (
            router1.router[l3_constants.SNAT_ROUTER_INTF_KEY])
        # Now save the internal device name to verify later
        internal_device_name = router1._get_snat_int_device_name(
            csnat_internal_port[0]['id'])
        self._assert_snat_namespace_exists(router1)
        qg_device, sg_device = self._get_dvr_snat_namespace_device_status(
            router1, internal_dev_name=internal_device_name)
        self.assertTrue(qg_device)
        self.assertTrue(sg_device)
        self.assertEqual(router1.snat_ports, snat_internal_port)
        # Now let us not pass INTERFACE_KEY, to emulate
        # the interface has been removed.
        router1.router[l3_constants.INTERFACE_KEY] = []
        # Now let us not pass the SNAT_ROUTER_INTF_KEY, to emulate
        # that the server did not send it, since the interface has been
        # removed.
        router1.router[l3_constants.SNAT_ROUTER_INTF_KEY] = []
        self.agent._process_updated_router(router1.router)
        router_updated = self.agent.router_info[router_info['id']]
        self._assert_snat_namespace_exists(router_updated)
        qg_device, sg_device = self._get_dvr_snat_namespace_device_status(
            router_updated, internal_dev_name=internal_device_name)
        self.assertFalse(sg_device)
        self.assertTrue(qg_device)

    def _mocked_dvr_ha_router(self, agent):
        r_info = self.generate_dvr_router_info(enable_ha=True,
                                               enable_snat=True,
                                               agent=agent)

        r_snat_ns_name = namespaces.build_ns_name(dvr_snat_ns.SNAT_NS_PREFIX,
                                                  r_info['id'])

        mocked_r_snat_ns_name = r_snat_ns_name + '@' + agent.host
        r_ns_name = namespaces.build_ns_name(namespaces.NS_PREFIX,
                                             r_info['id'])

        mocked_r_ns_name = r_ns_name + '@' + agent.host

        return r_info, mocked_r_ns_name, mocked_r_snat_ns_name

    def _setup_dvr_ha_agents(self):
        self.agent.conf.agent_mode = 'dvr_snat'

        conf = self._configure_agent('agent2')
        self.failover_agent = neutron_l3_agent.L3NATAgentWithStateReport(
            'agent2', conf)
        self.failover_agent.conf.agent_mode = 'dvr_snat'

    def _setup_dvr_ha_bridges(self):
        br_int_1 = self._get_agent_ovs_integration_bridge(self.agent)
        br_int_2 = self._get_agent_ovs_integration_bridge(self.failover_agent)

        veth1, veth2 = self.useFixture(net_helpers.VethFixture()).ports
        br_int_1.add_port(veth1.name)
        br_int_2.add_port(veth2.name)

    def _create_dvr_ha_router(self, agent):
        get_ns_name = mock.patch.object(namespaces.RouterNamespace,
                                        '_get_ns_name').start()
        get_snat_ns_name = mock.patch.object(dvr_snat_ns.SnatNamespace,
                                             'get_snat_ns_name').start()
        (r_info,
         mocked_r_ns_name,
         mocked_r_snat_ns_name) = self._mocked_dvr_ha_router(agent)
        get_ns_name.return_value = mocked_r_ns_name
        get_snat_ns_name.return_value = mocked_r_snat_ns_name
        router = self.manage_router(agent, r_info)
        return router

    def _assert_ip_addresses_in_dvr_ha_snat_namespace(self, router):
        namespace = router.ha_namespace
        ex_gw_port = router.get_ex_gw_port()
        snat_port = router.get_snat_interfaces()[0]
        ex_gw_port_name = router.get_external_device_name(
            ex_gw_port['id'])
        snat_port_name = router._get_snat_int_device_name(
            snat_port['id'])

        ip = ex_gw_port["fixed_ips"][0]['ip_address']
        prefix_len = ex_gw_port["fixed_ips"][0]['prefixlen']
        ex_gw_port_cidr = ip + "/" + str(prefix_len)
        ip = snat_port["fixed_ips"][0]['ip_address']
        prefix_len = snat_port["fixed_ips"][0]['prefixlen']
        snat_port_cidr = ip + "/" + str(prefix_len)

        self._assert_ip_address_on_interface(namespace,
                                             ex_gw_port_name,
                                             ex_gw_port_cidr)
        self._assert_ip_address_on_interface(namespace,
                                             snat_port_name,
                                             snat_port_cidr)

    def _assert_no_ip_addresses_in_dvr_ha_snat_namespace(self, router):
        namespace = router.ha_namespace
        ex_gw_port = router.get_ex_gw_port()
        snat_port = router.get_snat_interfaces()[0]
        ex_gw_port_name = router.get_external_device_name(
            ex_gw_port['id'])
        snat_port_name = router._get_snat_int_device_name(
            snat_port['id'])

        self._assert_no_ip_addresses_on_interface(namespace,
                                                  snat_port_name)
        self._assert_no_ip_addresses_on_interface(namespace,
                                                  ex_gw_port_name)

    def test_dvr_ha_router_failover(self):
        self._setup_dvr_ha_agents()
        self._setup_dvr_ha_bridges()

        router1 = self._create_dvr_ha_router(self.agent)
        router2 = self._create_dvr_ha_router(self.failover_agent)

        utils.wait_until_true(lambda: router1.ha_state == 'master')
        utils.wait_until_true(lambda: router2.ha_state == 'backup')

        self._assert_ip_addresses_in_dvr_ha_snat_namespace(router1)
        self._assert_no_ip_addresses_in_dvr_ha_snat_namespace(router2)

        self.fail_ha_router(router1)

        utils.wait_until_true(lambda: router2.ha_state == 'master')
        utils.wait_until_true(lambda: router1.ha_state == 'backup')

        self._assert_ip_addresses_in_dvr_ha_snat_namespace(router2)
        self._assert_no_ip_addresses_in_dvr_ha_snat_namespace(router1)

    def test_dvr_router_static_routes(self):
        """Test to validate the extra routes on dvr routers."""
        self.agent.conf.agent_mode = 'dvr_snat'
        router_info = self.generate_dvr_router_info(enable_snat=True)
        router1 = self.manage_router(self.agent, router_info)
        self.assertTrue(self._namespace_exists(router1.ns_name))
        self._assert_snat_namespace_exists(router1)
        snat_ns_name = dvr_snat_ns.SnatNamespace.get_snat_ns_name(
            router1.router_id)
        # Now try to add routes that are suitable for both the
        # router namespace and the snat namespace.
        router1.router['routes'] = [{'destination': '8.8.4.0/24',
                                     'nexthop': '35.4.0.20'}]
        self.agent._process_updated_router(router1.router)
        router_updated = self.agent.router_info[router_info['id']]
        self._assert_extra_routes(router_updated, namespace=snat_ns_name)
        self._assert_extra_routes(router_updated)

    def _assert_fip_namespace_deleted(self, ext_gateway_port):
        ext_net_id = ext_gateway_port['network_id']
        self.agent.fipnamespace_delete_on_ext_net(
            self.agent.context, ext_net_id)
        self._assert_interfaces_deleted_from_ovs()
        fip_ns_name = dvr_fip_ns.FipNamespace._get_ns_name(ext_net_id)
        self.assertFalse(self._namespace_exists(fip_ns_name))
