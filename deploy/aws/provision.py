"""Provision (or tear down) the PaperCI cloud server. Infrastructure as code for an account where
CloudFormation is not permitted (hackathon SCP), so this is an idempotent boto3 script instead.

    python deploy/aws/provision.py up --ssh-cidr 1.2.3.4/32 --pubkey ~/.ssh/paperci_aws.pub
    python deploy/aws/provision.py down

Account constraints found by dry runs (2026-10-11): only us-east-1; instance types up to t3.large; root volume
<= 50 GB; the CreatedBy tag is reserved (set by the organizers); CloudFormation, SSM, SES, Bedrock, Budgets and
Cost Explorer are denied. The organizers' VPC has public subnets routed to an internet gateway.

Choices: t3.large with CPU credits in `standard` mode (no surprise burst charges), 50 GB gp3 encrypted root,
IMDSv2 only, no instance profile (the agent needs no AWS API access), termination protection on (the account is
shared), shutdown = stop, an Elastic IP so the HTTPS hostname survives a stop/start. SSH only from the operator's
IP; 80/443 open for Caddy (automatic HTTPS). Secrets never go into user data (visible account-wide)."""
from __future__ import annotations

import argparse
import os
import sys
import time

import boto3

REGION = "us-east-1"
NAME = "paperci"
SUBNET_NAME = "Hackathon_VPC-subnet-public1-us-east-1a"
INSTANCE_TYPE = "t3.large"
DISK_GB = 50
UBUNTU_OWNER = "099720109477"
UBUNTU_NAME = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"


def _tag(name: str) -> list[dict]:
    return [{"Key": "Name", "Value": name}, {"Key": "Project", "Value": "paperci"}]


def up(ec2, ssh_cidr: str, pubkey_path: str) -> dict:
    sub = ec2.describe_subnets(Filters=[{"Name": "tag:Name", "Values": [SUBNET_NAME]}])["Subnets"][0]
    vpc = sub["VpcId"]
    sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": ["paperci-web"]},
                                                {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if sgs:
        sg = sgs[0]["GroupId"]
    else:
        sg = ec2.create_security_group(GroupName="paperci-web", Description="PaperCI - HTTPS public, SSH operator only",
                                       VpcId=vpc, TagSpecifications=[{"ResourceType": "security-group", "Tags": _tag("paperci-web")}])["GroupId"]
        ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": ssh_cidr, "Description": "operator SSH"}]},
            {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "ACME + redirect"}]},
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTPS"}]}])
    if not ec2.describe_key_pairs(Filters=[{"Name": "key-name", "Values": ["paperci-ops"]}])["KeyPairs"]:
        ec2.import_key_pair(KeyName="paperci-ops", PublicKeyMaterial=open(os.path.expanduser(pubkey_path), "rb").read())
    ami = sorted(ec2.describe_images(Owners=[UBUNTU_OWNER], Filters=[{"Name": "name", "Values": [UBUNTU_NAME]},
                                                                      {"Name": "state", "Values": ["available"]}])["Images"],
                 key=lambda i: i["CreationDate"])[-1]["ImageId"]
    live = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [NAME]},
                                           {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]}])
    inst = [i for r in live["Reservations"] for i in r["Instances"]]
    if inst:
        iid = inst[0]["InstanceId"]
    else:
        iid = ec2.run_instances(
            ImageId=ami, InstanceType=INSTANCE_TYPE, MinCount=1, MaxCount=1, KeyName="paperci-ops",
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": sub["SubnetId"], "Groups": [sg], "AssociatePublicIpAddress": True}],
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": DISK_GB, "VolumeType": "gp3",
                                                                    "Encrypted": True, "DeleteOnTermination": True}}],
            CreditSpecification={"CpuCredits": "standard"}, InstanceInitiatedShutdownBehavior="stop",
            DisableApiTermination=True, MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled"},
            TagSpecifications=[{"ResourceType": "instance", "Tags": _tag(NAME)},
                               {"ResourceType": "volume", "Tags": _tag(NAME + "-root")}])["Instances"][0]["InstanceId"]
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    addrs = ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [NAME]}])["Addresses"]
    if addrs:
        alloc = addrs[0]["AllocationId"]
    else:
        alloc = ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": _tag(NAME)}])["AllocationId"]
    ec2.associate_address(InstanceId=iid, AllocationId=alloc)
    ip = ec2.describe_addresses(AllocationIds=[alloc])["Addresses"][0]["PublicIp"]
    return {"instance": iid, "ip": ip, "host": ip.replace(".", "-") + ".sslip.io", "sg": sg, "ami": ami}


def down(ec2) -> dict:
    live = ec2.describe_instances(Filters=[{"Name": "tag:Name", "Values": [NAME]},
                                           {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]}])
    ids = [i["InstanceId"] for r in live["Reservations"] for i in r["Instances"]]
    for iid in ids:
        ec2.modify_instance_attribute(InstanceId=iid, DisableApiTermination={"Value": False})
        ec2.terminate_instances(InstanceIds=[iid])
    if ids:
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
    for a in ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [NAME]}])["Addresses"]:
        ec2.release_address(AllocationId=a["AllocationId"])
    for g in ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": ["paperci-web"]}])["SecurityGroups"]:
        for _ in range(10):
            try:
                ec2.delete_security_group(GroupId=g["GroupId"])
                break
            except Exception:
                time.sleep(10)
    ec2.delete_key_pair(KeyName="paperci-ops")
    return {"terminated": ids}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "down"])
    ap.add_argument("--ssh-cidr")
    ap.add_argument("--pubkey", default="~/.ssh/paperci_aws.pub")
    a = ap.parse_args()
    client = boto3.client("ec2", region_name=REGION)
    print(up(client, a.ssh_cidr, a.pubkey) if a.cmd == "up" else down(client))
    sys.exit(0)
