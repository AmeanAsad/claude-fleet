"""Pulumi program for Claude Fleet worker VMs.

Reads a 'provider' config key and delegates to the appropriate provider-specific
resource creation. Supports Azure and GCP.
"""

import json
from pathlib import Path

import pulumi


def _read_ssh_pub_key(ssh_key_path: str) -> str:
    pub_key_path = Path(f"{ssh_key_path}.pub").expanduser()
    try:
        return pub_key_path.read_text().strip()
    except FileNotFoundError:
        pulumi.log.warn(f"SSH public key not found at {pub_key_path}, using placeholder")
        return "ssh-ed25519 PLACEHOLDER"


def _create_azure_resources(workers: dict, azure_cfg: dict) -> None:
    import pulumi_azure_native as azure

    region = azure_cfg["region"]
    rg_name = azure_cfg["resource_group"]
    subscription_id = azure_cfg.get("subscription_id", "")
    ssh_user = azure_cfg["ssh_user"]
    ssh_key_path = azure_cfg["ssh_key"]
    default_instance_type = azure_cfg["instance_type"]
    image_cfg = azure_cfg["image"]

    ssh_pub_key = _read_ssh_pub_key(ssh_key_path)

    # Import the existing RG if subscription_id is available; otherwise create fresh.
    rg_opts = None
    if subscription_id:
        rg_id = f"/subscriptions/{subscription_id}/resourceGroups/{rg_name}"
        rg_opts = pulumi.ResourceOptions(import_=rg_id, retain_on_delete=True)

    resource_group = azure.resources.ResourceGroup(
        "fleet-rg",
        resource_group_name=rg_name,
        location=region,
        opts=rg_opts,
    )

    # Per-region networking (vnet, subnet, NSG)
    needed_regions = {region}
    for worker_cfg in workers.values():
        needed_regions.add(worker_cfg.get("region", region))

    region_nets: dict[str, tuple] = {}
    for idx, loc in enumerate(sorted(needed_regions)):
        suffix = loc.replace(" ", "").lower()
        prefix = f"10.{idx}.0.0/16"
        subnet_prefix = f"10.{idx}.1.0/24"

        vnet = azure.network.VirtualNetwork(
            f"fleet-vnet-{suffix}",
            resource_group_name=resource_group.name,
            location=loc,
            address_space=azure.network.AddressSpaceArgs(
                address_prefixes=[prefix],
            ),
        )

        subnet = azure.network.Subnet(
            f"fleet-subnet-{suffix}",
            resource_group_name=resource_group.name,
            virtual_network_name=vnet.name,
            address_prefix=subnet_prefix,
        )

        nsg = azure.network.NetworkSecurityGroup(
            f"fleet-nsg-{suffix}",
            resource_group_name=resource_group.name,
            location=loc,
            security_rules=[
                azure.network.SecurityRuleArgs(
                    name="SSH",
                    priority=1000,
                    direction="Inbound",
                    access="Allow",
                    protocol="Tcp",
                    source_port_range="*",
                    destination_port_range="22",
                    source_address_prefix="*",
                    destination_address_prefix="*",
                ),
                azure.network.SecurityRuleArgs(
                    name="CfleetServices",
                    priority=1100,
                    direction="Inbound",
                    access="Allow",
                    protocol="Tcp",
                    source_port_range="*",
                    destination_port_range="8000-9000",
                    source_address_prefix="*",
                    destination_address_prefix="*",
                ),
            ],
        )

        region_nets[loc] = (subnet, nsg)

    for name, worker_cfg in workers.items():
        instance_type = worker_cfg.get("instance_type", default_instance_type)
        vm_type = worker_cfg.get("vm_type", "regular")
        worker_location = worker_cfg.get("region", region)

        worker_subnet, worker_nsg = region_nets[worker_location]

        public_ip = azure.network.PublicIPAddress(
            f"{name}-ip",
            resource_group_name=resource_group.name,
            location=worker_location,
            public_ip_allocation_method="Static",
            sku=azure.network.PublicIPAddressSkuArgs(name="Standard"),
        )

        nic = azure.network.NetworkInterface(
            f"{name}-nic",
            resource_group_name=resource_group.name,
            location=worker_location,
            ip_configurations=[
                azure.network.NetworkInterfaceIPConfigurationArgs(
                    name="primary",
                    subnet=azure.network.SubnetArgs(id=worker_subnet.id),
                    public_ip_address=azure.network.PublicIPAddressArgs(id=public_ip.id),
                ),
            ],
            network_security_group=azure.network.NetworkSecurityGroupArgs(id=worker_nsg.id),
        )

        security_profile = None
        if vm_type in ("snp", "tdx"):
            security_profile = azure.compute.SecurityProfileArgs(
                security_type="ConfidentialVM",
                uefi_settings=azure.compute.UefiSettingsArgs(
                    secure_boot_enabled=True,
                    v_tpm_enabled=True,
                ),
            )

        if vm_type in ("snp", "tdx"):
            vm_image_ref = azure.compute.ImageReferenceArgs(
                publisher="Canonical",
                offer="0001-com-ubuntu-confidential-vm-jammy",
                sku="22_04-lts-cvm",
                version="latest",
            )
        else:
            vm_image_ref = azure.compute.ImageReferenceArgs(
                publisher=image_cfg["publisher"],
                offer=image_cfg["offer"],
                sku=image_cfg["sku"],
                version=image_cfg["version"],
            )

        if vm_type in ("snp", "tdx"):
            os_disk = azure.compute.OSDiskArgs(
                create_option="FromImage",
                managed_disk=azure.compute.ManagedDiskParametersArgs(
                    storage_account_type="StandardSSD_LRS",
                    security_profile=azure.compute.VMDiskSecurityProfileArgs(
                        security_encryption_type="VMGuestStateOnly",
                    ),
                ),
                disk_size_gb=64,
            )
        else:
            os_disk = azure.compute.OSDiskArgs(
                create_option="FromImage",
                managed_disk=azure.compute.ManagedDiskParametersArgs(
                    storage_account_type="StandardSSD_LRS",
                ),
                disk_size_gb=64,
            )

        vm_args = dict(
            resource_group_name=resource_group.name,
            location=worker_location,
            hardware_profile=azure.compute.HardwareProfileArgs(vm_size=instance_type),
            network_profile=azure.compute.NetworkProfileArgs(
                network_interfaces=[
                    azure.compute.NetworkInterfaceReferenceArgs(id=nic.id),
                ],
            ),
            os_profile=azure.compute.OSProfileArgs(
                computer_name=name,
                admin_username=ssh_user,
                linux_configuration=azure.compute.LinuxConfigurationArgs(
                    disable_password_authentication=True,
                    ssh=azure.compute.SshConfigurationArgs(
                        public_keys=[
                            azure.compute.SshPublicKeyArgs(
                                path=f"/home/{ssh_user}/.ssh/authorized_keys",
                                key_data=ssh_pub_key,
                            ),
                        ],
                    ),
                ),
            ),
            storage_profile=azure.compute.StorageProfileArgs(
                image_reference=vm_image_ref,
                os_disk=os_disk,
            ),
            tags={"fleet": "true", "worker": name, "vm_type": vm_type},
        )
        if security_profile:
            vm_args["security_profile"] = security_profile

        vm = azure.compute.VirtualMachine(f"{name}-vm", **vm_args)

        pulumi.export(f"{name}_ip", public_ip.ip_address)
        pulumi.export(f"{name}_id", vm.id)


def _create_gcp_resources(workers: dict, gcp_cfg: dict) -> None:
    import pulumi_gcp as gcp

    project_id = gcp_cfg["project_id"]
    default_zone = gcp_cfg["zone"]
    region = gcp_cfg["region"]
    ssh_user = gcp_cfg["ssh_user"]
    ssh_key_path = gcp_cfg["ssh_key"]
    default_instance_type = gcp_cfg["instance_type"]
    image_cfg = gcp_cfg["image"]

    ssh_pub_key = _read_ssh_pub_key(ssh_key_path)

    # Shared VPC network and firewall
    network = gcp.compute.Network(
        "fleet-network",
        project=project_id,
        auto_create_subnetworks=False,
    )

    subnet = gcp.compute.Subnetwork(
        "fleet-subnet",
        project=project_id,
        region=region,
        network=network.id,
        ip_cidr_range="10.0.1.0/24",
    )

    gcp.compute.Firewall(
        "fleet-allow-ssh",
        project=project_id,
        network=network.id,
        allows=[gcp.compute.FirewallAllowArgs(
            protocol="tcp",
            ports=["22"],
        )],
        source_ranges=["0.0.0.0/0"],
        target_tags=["fleet-worker"],
    )

    gcp.compute.Firewall(
        "fleet-allow-cfleet",
        project=project_id,
        network=network.id,
        allows=[gcp.compute.FirewallAllowArgs(
            protocol="tcp",
            ports=["8000-9000"],
        )],
        source_ranges=["0.0.0.0/0"],
        target_tags=["fleet-worker"],
    )

    for name, worker_cfg in workers.items():
        instance_type = worker_cfg.get("instance_type", default_instance_type)
        vm_type = worker_cfg.get("vm_type", "regular")
        # Engine passes "region" as the override key; for GCP treat it as zone
        worker_zone = worker_cfg.get("zone", worker_cfg.get("region", default_zone))

        is_cvm = vm_type in ("snp", "tdx")
        if is_cvm:
            boot_image = "ubuntu-os-cloud/ubuntu-2204-lts"
        else:
            boot_image = f"{image_cfg['project']}/{image_cfg['family']}"

        instance_args = dict(
            project=project_id,
            zone=worker_zone,
            machine_type=instance_type,
            boot_disk=gcp.compute.InstanceBootDiskArgs(
                initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
                    image=boot_image,
                    size=64,
                    type="pd-ssd",
                ),
            ),
            network_interfaces=[gcp.compute.InstanceNetworkInterfaceArgs(
                subnetwork=subnet.id,
                access_configs=[gcp.compute.InstanceNetworkInterfaceAccessConfigArgs()],
            )],
            metadata={
                "ssh-keys": f"{ssh_user}:{ssh_pub_key}",
            },
            tags=["fleet-worker"],
            labels={"fleet": "true", "worker": name, "vm-type": vm_type},
        )

        if is_cvm:
            cvm_type = "SEV_SNP" if vm_type == "snp" else "TDX"
            instance_args["confidential_instance_config"] = gcp.compute.InstanceConfidentialInstanceConfigArgs(
                enable_confidential_compute=True,
                confidential_instance_type=cvm_type,
            )
            instance_args["scheduling"] = gcp.compute.InstanceSchedulingArgs(
                on_host_maintenance="TERMINATE",
            )
            if vm_type == "snp":
                instance_args["min_cpu_platform"] = "AMD Milan"

        instance = gcp.compute.Instance(f"{name}-vm", **instance_args)

        pulumi.export(f"{name}_ip", instance.network_interfaces[0].access_configs[0].nat_ip)
        pulumi.export(f"{name}_id", instance.id)


def pulumi_program() -> None:
    """Inline Pulumi program — called directly by the automation API."""
    config = pulumi.Config("claude-fleet")

    workers_raw = config.get("workers") or "{}"
    workers = json.loads(workers_raw)

    # Group workers by provider
    azure_workers = {}
    gcp_workers = {}
    for name, w_cfg in workers.items():
        w_provider = w_cfg.get("provider", "azure")
        if w_provider == "gcp":
            gcp_workers[name] = w_cfg
        else:
            azure_workers[name] = w_cfg

    if azure_workers:
        azure_cfg_raw = config.get("azure") or "{}"
        azure_cfg = json.loads(azure_cfg_raw)
        if not azure_cfg:
            pulumi.log.warn("Azure workers requested but no azure config found")
        else:
            _create_azure_resources(azure_workers, azure_cfg)

    if gcp_workers:
        gcp_cfg_raw = config.get("gcp") or "{}"
        gcp_cfg = json.loads(gcp_cfg_raw)
        if not gcp_cfg:
            pulumi.log.warn("GCP workers requested but no gcp config found")
        else:
            _create_gcp_resources(gcp_workers, gcp_cfg)
