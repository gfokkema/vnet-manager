from pylxd.exceptions import NotFound
from sys import modules
from logging import getLogger
from tabulate import tabulate
from time import sleep

from vnet_manager.conf import settings
from vnet_manager.providers.lxc import get_lxd_client

logger = getLogger(__name__)


def show_status(config):
    """
    Print a table with the current machine statuses
    :param dict config: The config provided by vnet_manager.config.get_config()
    """
    logger.info("Listing machine statuses")
    header = ["Name", "Status", "Provider"]
    statuses = []
    for name, info in config["machines"].items():
        provider = settings.MACHINE_TYPE_PROVIDER_MAPPING[info["type"]]
        # Call the relevant provider get_%s_machine_status function
        statuses.append(getattr(modules[__name__], "get_{}_machine_status".format(provider))(name))
    print(tabulate(statuses, headers=header, tablefmt="pretty"))


def check_if_lxc_machine_exists(machine):
    """
    Checks if an LXC machine exists
    :param str machine: The machine/container to check for
    :return: bool: True if it exists, false otherwise
    """
    return get_lxd_client().containers.exists(machine)


def get_lxc_machine_status(name):
    """
    Gets the LXC machine state and returns a list
    :param name: str: The name of the machine
    :return: list: [name, state, provider]
    """
    client = get_lxd_client()
    try:
        status = client.containers.get(name).status
    except NotFound:
        status = "NA"
    return [name, status, "LXC"]


def wait_for_lxc_machine_status(container, status):
    """
    Waits for a LXC machine to converge to the requested status
    :param pylxd.client.Client.container() container: The container to wait for
    :param str status: The status to wait for
    :raise TimeoutError if wait time expires
    """
    logger.debug("Waiting for LXC container {} to get a {} status".format(container.name, status))
    for i in range(1, settings.LXC_MAX_STATUS_WAIT_ATTEMPTS):
        # Actually ask for the container.state().status, because container.status is a static value
        if container.state().status.lower() == status.lower():
            logger.debug("Container successfully converged to {} status".format(status))
            return
        else:
            logger.info(
                "Container {} not yet in {} status, waiting for {} seconds".format(container.name, status, settings.LXC_STATUS_WAIT_SLEEP)
            )
            sleep(settings.LXC_STATUS_WAIT_SLEEP)
    raise TimeoutError("Wait time for container {} to converge to {} status expired, giving up".format(container.name, status))


def change_machine_status(config, status="stop", machines=None):
    """
    Change the status of the passed machines to the requested state
    :param dict config: The config provided by get_config()
    :param str status: The status to change the machine to
    :param list machines: A list of machine names to stop/start, if None all will be changed
    """
    # Check for valid status change
    if status not in settings.VALID_STATUSES:
        raise NotImplementedError("Requested machine status change {} unknown".format(status))

    # Get all the machines from the config if not already provided
    machines = machines if machines else config["machines"].keys()

    # For each machine get the provider and execute the relevant status change function
    for machine in machines:
        # First check if the machine exists
        if machine not in config["machines"]:
            logger.error("Tried to {} machine {}, but there is no config entry for it, skipping...".format(status, machine))
            continue
        # Get the provider
        provider = settings.MACHINE_TYPE_PROVIDER_MAPPING[config["machines"][machine]["type"]]
        # Call the provider change_status function
        logger.info("{} machine {} with provider {}".format("Starting" if status == "start" else "Stopping", machine, provider))
        getattr(modules[__name__], "change_{}_machine_status".format(provider))(machine, status=status)


def change_lxc_machine_status(machine, status="stop"):
    """
    Start a LXC machine
    :param str machine: The name of the machine to change the status of
    :param str status: The status to change the LXC machine to
    """
    client = get_lxd_client()
    try:
        machine = client.containers.get(machine)
    except NotFound:
        logger.error("Tried to change machine status of LXC container {}, but it doesn't exist!")
        return
    # Change the status
    if status == "stop":
        machine.stop()
    elif status == "start":
        machine.start()
    # Take a short nap after issuing the start/stop command, so we might pass the first status check
    sleep(1)
    try:
        required_state = "Stopped" if status == "stop" else "Running"
        wait_for_lxc_machine_status(machine, required_state)
        logger.debug("LXC container {} is running".format(machine.name))
    except TimeoutError:
        logger.error("Unable to change LXC status container {}, got timeout after issuing {} command".format(machine.name, status))


def create_machines(config, machines=None):
    """
    Meta function to call the other machine creation functions per provider
    :param dict config: The config generated by get_config()
    :param list machines: A list of machine to create, defaults to all machines in the config
    """
    # Get all the machines from the config if not already provided
    machines = machines if machines else config["machines"].keys()
    create_lxc_machines_from_base_image(config, machines)


def create_lxc_machines_from_base_image(config, containers):
    """
    Create LXC machines from the base image specified in the settings
    :param dict config: The config generated by get_config()
    :param list containers: A list of machines to create
    """
    containers_to_create = []

    # Get all the LXC machines to create
    for container in containers:
        # Check if the requested machine name is present in the config
        if container not in config["machines"]:
            logger.error(
                "Tried to get provider for container {}, but the container was not found in the config, skipping".format(container)
            )
        # Quick check if the machine already exists
        elif check_if_lxc_machine_exists(container):
            logger.error("A LXC container with the name {} already exists, skipping".format(container))
        # Check if LXC is the provider
        elif settings.MACHINE_TYPE_PROVIDER_MAPPING[config["machines"][container]["type"]].lower() == "lxc":
            logger.debug("Selecting LXC machine {} for creation".format(container))
            containers_to_create.append(container)
        else:
            logger.debug("Machine {} is not provided by LXC, skipping LXC container creation".format(container))

    # Create it
    client = get_lxd_client()
    for container in containers_to_create:
        logger.debug("Generating LXC config for container {}".format(container))
        # Interface config
        # First add eth0 (default), which does nothing
        device_config = {"eth0": {"type": "none"}}
        # Then for each interface in the config add the configuration for that interface to the interfaces_config dict
        for inet_name, inet_config in config["machines"][container]["interfaces"].items():
            device_config[inet_name] = {
                "name": inet_name,  # The name of the interface inside the instance
                "host_name": "{}-{}".format(container, inet_name),  # The name of the interface inside the host
                "parent": "{}{}".format(settings.VNET_BRIDGE_NAME, inet_config["bridge"]),  # The name of the host device
                "type": "nic",
                "nictype": "bridged",
            }
        container_config = {
            "name": container,
            "source": {"alias": settings.LXC_BASE_IMAGE_ALIAS, "type": "image"},
            "ephemeral": False,
            "config": {"user.network-config": "disabled"},
            "devices": device_config,
        }
        logger.info("Creating LXC container {}".format(container))
        # TODO: Make this nicer by not waiting here but doing the configuration after we've created all containers
        client.containers.create(container_config, wait=True)