"""Pulumi program for Claude Fleet worker VMs.

Reads a 'workers' config map and creates one Azure VM per entry.
Networking infrastructure (vnet, subnet, NSG) is created per-region so workers
can be deployed across different Azure regions within a single resource group.
"""

import json
from pathlib import Path

import pulumi
import pulumi_azure_native as azure


def pulumi_program() -> None:
    """Inline Pulumi program — called directly by the automation API."""
    config = pulumi.Config("claude-fleet")

    workers_raw = config.get("workers") or "{}"
    workers = json.loads(workers_raw)

    azure_cfg_raw = config.get("azure") or "{}"
    azure_cfg = json.loads(azure_cfg_raw)

    # If there's no azure config yet (e.g., during init), skip everything
    if not azure_cfg:
        pulumi.log.info("No azure config found, skipping resource creation")
        return

    region = azure_cfg["region"]
    rg_name = azure_cfg["resource_group"]
    ssh_user = azure_cfg["ssh_user"]
    ssh_key_path = azure_cfg["ssh_key"]
    default_instance_type = azure_cfg["instance_type"]
    image_cfg = azure_cfg["image"]

    # Read SSH public key
    pub_key_path = Path(f"{ssh_key_path}.pub").expanduser()
    if pub_key_path.exists():
        ssh_pub_key = pub_key_path.read_text().strip()
    else:
        pulumi.log.warn(f"SSH public key not found at {pub_key_path}, using placeholder")
        ssh_pub_key = "ssh-ed25519 PLACEHOLDER"

    # =========================================================================
    # Shared resource group (location is just metadata for the RG itself)
    # =========================================================================

    resource_group = azure.resources.ResourceGroup(
        "fleet-rg",
        resource_group_name=rg_name,
        location=region,
    )

    # =========================================================================
    # Per-region networking (vnet, subnet, NSG)
    # =========================================================================

    # Collect all regions needed by workers
    needed_regions = {region}  # always include default region
    for worker_cfg in workers.values():
        needed_regions.add(worker_cfg.get("region", region))

    # Use a /16 per region with different second octets to avoid overlap
    region_nets: dict[str, tuple] = {}  # region -> (subnet, nsg)
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
                    # TODO: lock to user's IP via --my-ip flag on fleet init
                    source_address_prefix="*",
                    destination_address_prefix="*",
                ),
            ],
        )

        region_nets[loc] = (subnet, nsg)

    # =========================================================================
    # Per-worker resources
    # =========================================================================

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
            sku=azure.network.PublicIPAddressSkuArgs(
                name="Standard",
            ),
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

        # Confidential VM settings for SNP/TDX
        security_profile = None
        if vm_type in ("snp", "tdx"):
            security_profile = azure.compute.SecurityProfileArgs(
                security_type="ConfidentialVM",
                uefi_settings=azure.compute.UefiSettingsArgs(
                    secure_boot_enabled=True,
                    v_tpm_enabled=True,
                ),
            )

        # CVM images differ from regular — use confidential-capable Ubuntu image
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

        # CVM requires VMGS-encrypted OS disk
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
            hardware_profile=azure.compute.HardwareProfileArgs(
                vm_size=instance_type,
            ),
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
