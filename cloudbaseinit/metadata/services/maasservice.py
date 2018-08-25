# Copyright 2014 Cloudbase Solutions Srl
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
import re
import sys

import json
from oauthlib import oauth1
from oslo_log import log as oslo_logging
import requests

from cloudbaseinit import conf as cloudbaseinit_conf
from cloudbaseinit import exception
from cloudbaseinit.metadata.services import base
from cloudbaseinit.models import network as network_model
from cloudbaseinit.utils import x509constants

CONF = cloudbaseinit_conf.CONF
LOG = oslo_logging.getLogger(__name__)

MAAS_CONFIG_TYPE_PHYSICAL = "physical"
MAAS_CONFIG_TYPE_BOND = "bond"
MAAS_CONFIG_TYPE_VLAN = "vlan"
MAAS_CONGIG_TYPE_NAMESERVER = "nameserver"

MAAS_BOND_LACP_RATE_SLOW = "slow"
MAAS_BOND_LACP_RATE_FAST = "fast"

MAAS_SUBNET_TYPE_STATIC = "static"
MAAS_SUBNET_TYPE_MANUAL = "manual"

LINK_TYPE_MAP = {
    MAAS_CONFIG_TYPE_PHYSICAL: network_model.LINK_TYPE_PHYSICAL,
    MAAS_CONFIG_TYPE_BOND: network_model.LINK_TYPE_BOND,
    MAAS_CONFIG_TYPE_VLAN: network_model.LINK_TYPE_VLAN,
}

BOND_LACP_RATE_MAP = {
    MAAS_BOND_LACP_RATE_SLOW: network_model.BOND_LACP_RATE_SLOW,
    MAAS_BOND_LACP_RATE_FAST: network_model.BOND_LACP_RATE_FAST,
}


class _Realm(str):
    # There's a bug in oauthlib which ignores empty realm strings,
    # by checking that the given realm is always True.
    # This string class always returns True in a boolean context,
    # making sure that an empty realm can be used by oauthlib.
    def __bool__(self):
        return True

    __nonzero__ = __bool__


class MaaSHttpService(base.BaseHTTPMetadataService):
    _METADATA_2012_03_01 = '2012-03-01'

    def __init__(self):
        super(MaaSHttpService, self).__init__(
            base_url=CONF.maas.metadata_base_url,
            https_allow_insecure=CONF.maas.https_allow_insecure,
            https_ca_bundle=CONF.maas.https_ca_bundle)
        self._enable_retry = True
        self._metadata_version = self._METADATA_2012_03_01

    def load(self):
        super(MaaSHttpService, self).load()

        if not CONF.maas.metadata_base_url:
            LOG.debug('MaaS metadata url not set')
        else:
            try:
                self._get_cache_data('%s/meta-data/' % self._metadata_version)
                return True
            except Exception as ex:
                LOG.exception(ex)
                LOG.debug('Metadata not found at URL \'%s\'' %
                          CONF.maas.metadata_base_url)
        return False

    def _get_oauth_headers(self, url):
        LOG.debug("Getting authorization headers for %s.", url)
        client = oauth1.Client(
            CONF.maas.oauth_consumer_key,
            client_secret=CONF.maas.oauth_consumer_secret,
            resource_owner_key=CONF.maas.oauth_token_key,
            resource_owner_secret=CONF.maas.oauth_token_secret,
            signature_method=oauth1.SIGNATURE_PLAINTEXT)
        realm = _Realm("")
        headers = client.sign(url, realm=realm)[1]
        return headers

    def _http_request(self, url, data=None, headers=None):
        """Get content for received url."""
        if not url.startswith("http"):
            url = requests.compat.urljoin(self._base_url, url)
        headers = {} if headers is None else headers
        headers.update(self._get_oauth_headers(url))

        return super(MaaSHttpService, self)._http_request(url, data, headers)

    def get_host_name(self):
        return self._get_cache_data('%s/meta-data/local-hostname' %
                                    self._metadata_version, decode=True)

    def get_instance_id(self):
        return self._get_cache_data('%s/meta-data/instance-id' %
                                    self._metadata_version, decode=True)

    def get_public_keys(self):
        return self._get_cache_data('%s/meta-data/public-keys' %
                                    self._metadata_version,
                                    decode=True).splitlines()

    def get_client_auth_certs(self):
        certs_data = self._get_cache_data('%s/meta-data/x509' %
                                          self._metadata_version,
                                          decode=True)
        pattern = r"{begin}[\s\S]+?{end}".format(
            begin=x509constants.PEM_HEADER,
            end=x509constants.PEM_FOOTER)
        return re.findall(pattern, certs_data)

    def get_user_data(self):
        return self._get_cache_data('%s/user-data' % self._metadata_version)

    @staticmethod
    def _get_network_data():
        if sys.platform != "win32":
            return

        path = os.path.join(
            os.environ["systemdrive"], "\\curtin\\network.json")
        if not os.path.isfile(path):
            path = os.path.join(os.environ["systemdrive"], "\\network.json")
            if not os.path.isfile(path):
                path = None

        if path:
            json_data = open(path, "rb").read()
            return json.loads(json_data.decode('utf-8'))

    @staticmethod
    def _parse_config_link(config):
        link_id = config.get("id")
        name = config.get("name")
        mac = config.get("mac_address")
        mtu = config.get("mtu")
        maas_link_type = config.get("type")
        subnets = config.get("subnets", [])
        params = config.get("params", {})

        link_type = LINK_TYPE_MAP.get(maas_link_type)
        if link_type is None:
            raise exception.CloudbaseInitException(
                "Unsupported MAAS link type: %s" % maas_link_type)

        bond = None
        vlan_id = None
        vlan_link = None
        if maas_link_type == MAAS_CONFIG_TYPE_BOND:
            bond_interfaces = config.get("bond_interfaces")
            bond_mode = params.get("bond-mode")
            bond_xmit_hash_policy = params.get("bond-xmit-hash-policy")
            maas_bond_lacp_rate = params.get("bond-lacp-rate")

            if bond_mode not in network_model.AVAILABLE_BOND_TYPES:
                raise exception.CloudbaseInitException(
                    "Unsupported bond mode: %s" % bond_mode)

            if (bond_xmit_hash_policy is not None and
                    bond_xmit_hash_policy not in
                    network_model.AVAILABLE_BOND_LB_ALGORITHMS):
                raise exception.CloudbaseInitException(
                    "Unsupported bond hash policy: %s" % bond_xmit_hash_policy)

            bond = network_model.Bond(
                members=bond_interfaces,
                type=bond_mode,
                lb_algorithm=bond_xmit_hash_policy,
                lacp_rate=BOND_LACP_RATE_MAP.get(maas_bond_lacp_rate))
        elif link_type == MAAS_CONFIG_TYPE_VLAN:
            vlan_link = config.get("vlan_link")
            vlan_id = config.get("vlan_id")

        link = network_model.Link(
            id=link_id,
            name=name,
            type=link_type,
            mac_address=mac,
            mtu=mtu,
            bond=bond,
            vlan_id=vlan_id,
            vlan_link=vlan_link)

        network = None
        subnets = config.get("subnets", [])
        for subnet in subnets:
            maas_subnet_type = subnet.get("type")
            if maas_subnet_type == MAAS_SUBNET_TYPE_STATIC:
                address_cidr = subnet.get("address")
                gateway = subnet.get("gateway")
                dns_nameservers = subnet.get("dns_nameservers")

                # TODO(alexpilotti): Add support for extra routes
                routes = [
                    network_model.Route(
                        network_cidr=u"0.0.0.0/0",
                        gateway=gateway
                    )
                ]
                network = network_model.Network(
                    link=link_id,
                    address_cidr=address_cidr,
                    dns_nameservers=dns_nameservers,
                    routes=routes,
                )

        return link, network

    @staticmethod
    def _parse_config_nameserver(config):
        search = config.get("search", [])
        addresses = config.get("address")
        if addresses is None:
            raise exception.CloudbaseInitException(
                "MAAS nameserver configuration does not contain any address")

        return network_model.NameServerService(
            addresses=addresses,
            search=search)

    @staticmethod
    def _parse_config_item(config):
        link = None
        network = None
        service = None

        config_type = config.get("type")
        if config_type == MAAS_CONGIG_TYPE_NAMESERVER:
            service = MaaSHttpService._parse_config_nameserver(config)
        else:
            link, network = MaaSHttpService._parse_config_link(config)

        return link, network, service

    def get_network_details_v2(self):
        network_data = self._get_network_data()
        if not network_data:
            return

        version = network_data.get("version")
        if version != 1:
            LOG.warn('Unsupported MAAS network metadata version: %s', version)
            return

        links = []
        networks = []
        services = []

        config = network_data.get("config", [])
        for config_item in config:
            link, network, service = self._parse_config_item(config_item)
            if link:
                links.append(link)
            if network:
                networks.append(network),
            if service:
                services.append(service)

        return network_model.NetworkDetailsV2(
            links=links,
            networks=networks,
            services=services
        )
