"""Pulumi program for Open Fleet worker VMs.

Reads a 'workers' config map and creates one Azure VM per entry.
Shared infrastructure (resource group, vnet, subnet, NSG) is created once
and used by all workers.
"""

import json
from pathlib import Path

import pulumi
import pulumi_azure_native as azure

config = pulumi.Config("open-fleet")

workers_raw = config.get("workers") or "{}"
workers = json.loads(workers_raw)

azure_cfg_raw = config.get("azure") or "{}"
azure_cfg = json.loads(azure_cfg_raw)

# If there's no azure config yet (e.g., during init), skip everything
if not azure_cfg:
    pulumi.log.info("No azure config found, skipping resource creation")
else:
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
    # Shared infrastructure
    # =========================================================================

    resource_group = azure.resources.ResourceGroup(
        "fleet-rg",
        resource_group_name=rg_name,
        location=region,
    )

    vnet = azure.network.VirtualNetwork(
        "fleet-vnet",
        resource_group_name=resource_group.name,
        location=resource_group.location,
        address_space=azure.network.AddressSpaceArgs(
            address_prefixes=["10.0.0.0/16"],
        ),
    )

    subnet = azure.network.Subnet(
        "fleet-subnet",
        resource_group_name=resource_group.name,
        virtual_network_name=vnet.name,
        address_prefix="10.0.1.0/24",
    )

    nsg = azure.network.NetworkSecurityGroup(
        "fleet-nsg",
        resource_group_name=resource_group.name,
        location=resource_group.location,
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

    # =========================================================================
    # Per-worker resources
    # =========================================================================

    for name, worker_cfg in workers.items():
        instance_type = worker_cfg.get("instance_type", default_instance_type)
        vm_type = worker_cfg.get("vm_type", "regular")

        public_ip = azure.network.PublicIPAddress(
            f"{name}-ip",
            resource_group_name=resource_group.name,
            location=resource_group.location,
            public_ip_allocation_method="Static",
            sku=azure.network.PublicIPAddressSkuArgs(
                name="Standard",
            ),
        )

        nic = azure.network.NetworkInterface(
            f"{name}-nic",
            resource_group_name=resource_group.name,
            location=resource_group.location,
            ip_configurations=[
                azure.network.NetworkInterfaceIPConfigurationArgs(
                    name="primary",
                    subnet=azure.network.SubnetArgs(id=subnet.id),
                    public_ip_address=azure.network.PublicIPAddressArgs(id=public_ip.id),
                ),
            ],
            network_security_group=azure.network.NetworkSecurityGroupArgs(id=nsg.id),
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
            location=resource_group.location,
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
