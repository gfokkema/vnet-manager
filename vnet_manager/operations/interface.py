import shlex
from pyroute2 import IPRoute
from logging import getLogger
from subprocess import check_call, CalledProcessError, Popen, DEVNULL
from os.path import join
from psutil import process_iter
from tabulate import tabulate

from vnet_manager.conf import settings
from vnet_manager.utils.mac import random_mac_generator

logger = getLogger(__name__)


def get_vnet_interface_names_from_config(config):
    """
    Gets the VNet inetface names from the config
    :param dict config: The conifg generated by get_config()
    :return: list: The VNet interface names
    """
    return [settings.VNET_BRIDGE_NAME + str(i) for i in range(0, config["switches"])]


def get_machines_by_vnet_interface_name(config, ifname):
    """
    Returns a list of machine that use a particular VNet interface
    :param dict config: The config generated by get_config()
    :param str ifname: The interface to check for
    :return: list of VNet machines using that interface
    """
    machines = []
    for m_name, m_data in config["machines"].items():
        for int_data in m_data["interfaces"].values():
            if int(int_data["bridge"]) == int(ifname[-1]):
                machines.append(m_name)
    return machines


def show_vnet_interface_status(config):
    """
    Shows the VNet interface status to the user
    :param dict config: The config generated by get_config()
    """
    logger.info("Listing VNet interface statuses")
    header = ["Name", "Status", "L2_addr", "Sniffer active", "Used by"]
    statuses = []
    ip = IPRoute()
    for ifname in get_vnet_interface_names_from_config(config):
        used_by = get_machines_by_vnet_interface_name(config, ifname)
        devs = ip.link_lookup(ifname=ifname)
        if not devs:
            # Link does not exist
            statuses.append([ifname, "NA", "NA", "NA", ", ".join(used_by)])
        else:
            # Get the link info
            info = ip.link("get", index=devs[0])
            l2_addr = [addr[1] for addr in info[0]["attrs"] if addr[0] == "IFLA_ADDRESS"][0]
            state = info[0]["state"]
            sniffer = check_if_sniffer_exists(ifname)
            statuses.append([ifname, state, l2_addr, sniffer, ", ".join(used_by)])
    print(tabulate(statuses, headers=header, tablefmt="pretty"))


def check_if_interface_exists(ifname):
    """
    Check if an interface exists
    :param str ifname: The interface name to check for
    :return: bool: True if the interface exists, False otherwise
    """
    iface = IPRoute().link_lookup(ifname=ifname)
    return True if iface else False


def create_vnet_interface(ifname):
    """
    Creates a VNet bridge interface
    :param str ifname: The name of the interface to create
    """
    logger.info("Creating VNet bridge interface {}".format(ifname))
    ip = IPRoute()
    ip.link("add", ifname=ifname, kind="bridge")
    # Bring up the interface
    configure_vnet_interface(ifname)


def create_vnet_interface_iptables_rules(ifname):
    """
    VNet interfaces should act as dump bridges and should not have any connectivity to the outside world
    So this function makes some IPtables rules to make sure the VNet interface cannot talk to the outside.
    :param str ifname: The interface the create IPtables rules for
    """
    rule = "OUTPUT -o {} -j DROP".format(ifname)
    # First we check if the rule already exists
    try:
        check_call(shlex.split("iptables -C {}".format(rule)), stderr=DEVNULL)
        logger.debug("IPtables DROP rule for VNet interface {} already exists, skipping creation".format(ifname))
    except CalledProcessError:
        logger.info("Creating IPtables DROP rule to the outside world for VNet interface {}".format(ifname))
        try:
            check_call(shlex.split("iptables -A {}".format(rule)))
        except CalledProcessError as e:
            logger.error("Unable to create IPtables rule, got output: {}".format(e.output))


def configure_vnet_interface(ifname):
    """
    Configures an vnet interface to be in the correct state for forwarding vnet machine traffic
    :param str ifname: The vnet interface to configure
    """
    ip = IPRoute()
    dev = ip.link_lookup(ifname=ifname)[0]
    # Make sure it's set to down state
    ip.link("set", index=dev, state="down")
    # Set the mac
    ip.link("set", index=dev, address=random_mac_generator())
    # Bring up the interface
    ip.link("set", index=dev, state="up")


def bring_up_vnet_interfaces(config, sniffer=False):
    """
    Check the status of the vnet interfaces defined in the config and brings up the interfaces if needed
    :param dict config: The config generated by get_config()
    :param bool sniffer: Check for a sniffer process and create it if it does not exist
    """
    ip = IPRoute()
    for ifname in get_vnet_interface_names_from_config(config):
        if not check_if_interface_exists(ifname):
            create_vnet_interface(ifname)
        # Block traffic to the outside world
        create_vnet_interface_iptables_rules(ifname)
        # Make sure the interface is up
        ip.link("set", ifname=ifname, state="up")
        if sniffer and not check_if_sniffer_exists(ifname):
            # Create it
            start_tcpdump_on_vnet_interface(ifname)


def check_if_sniffer_exists(ifname):
    """
    Check if there is already a sniffer running for a VNet interface
    :param str ifname: The VNet interface name to check
    :return bool: True if it exists, False otherwise
    """
    for process in process_iter():
        process_line = process.cmdline()
        if "tcpdump" in process_line and ifname in process_line:
            logger.debug("A TCPdump sniffer for interface {} already exists".format(ifname))
            return True
    return False


def bring_down_vnet_interfaces(config):
    """
    Brings down the VNet interfaces defined in the config
    This will automatically kill any attached sniffer processes
    :param dict config: The config generated by get_config()
    """
    ip = IPRoute()
    for ifname in get_vnet_interface_names_from_config(config):
        # Set the interface to down status
        if check_if_interface_exists(ifname):
            logger.info("Bringing down VNet interface {}".format(ifname))
            ip.link("set", ifname=ifname, state="down")
        else:
            # Device doesn't exist
            logger.warning("Tried to bring down VNet interface {}, but the interface doesn't exist".format(ifname))


def delete_vnet_interfaces(config):
    """
    Delete the VNet interfaces defined in the config
    :param config:
    :return:
    """
    ip = IPRoute()
    for ifname in get_vnet_interface_names_from_config(config):
        # Delete the interface
        if check_if_interface_exists(ifname):
            logger.info("Deleting VNet interface {}".format(ifname))
            ip.link("del", ifname=ifname)
        else:
            # Device doesn't exist
            logger.info("Tried to delete VNet interface {}, but it is already gone. That's okay".format(ifname))


def start_tcpdump_on_vnet_interface(ifname):
    """
    Starts a tcpdump process on a vnet interface
    :param str ifname: The interface to start the tcpdump on
    """
    path = join(settings.VNET_SNIFFER_PCAP_DIR, "{}.pcap".format(ifname))
    logger.info("Starting sniffer on VNet interface {}, PCAP location: {}".format(ifname, path))
    Popen(shlex.split("tcpdump -i {} -U -w {}".format(ifname, path)))
