#!/bin/bash
# Note: execute this file from the project root directory

#####
#
# Make sure we can build an image and deploy it inside Azure!
#
#####

set -e

. /usr/share/beakerlib/beakerlib.sh
. $(dirname $0)/lib/lib.sh

CLI="${CLI:-./src/bin/composer-cli}"


rlJournalStart
    rlPhaseStartSetup
        # NOTE: see test/README.md for information how to obtain these
        # UUIDs and what configuration is expected on the Azure side
        if [ -z "$AZURE_SUBSCRIPTION_ID" ]; then
            rlFail "AZURE_SUBSCRIPTION_ID is empty!"
        else
            rlLogInfo "AZURE_SUBSCRIPTION_ID is configured"
        fi

        if [ -z "$AZURE_TENANT" ]; then
            rlFail "AZURE_TENANT is empty!"
        else
            rlLogInfo "AZURE_TENANT is configured"
        fi

        if [ -z "$AZURE_CLIENT_ID" ]; then
            rlFail "AZURE_CLIENT_ID is empty!"
        else
            rlLogInfo "AZURE_CLIENT_ID is configured"
        fi

        if [ -z "$AZURE_SECRET" ]; then
            rlFail "AZURE_SECRET is empty!"
        else
            rlLogInfo "AZURE_SECRET is configured"
        fi

        export AZURE_RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-composer}"
        rlLogInfo "AZURE_RESOURCE_GROUP=$AZURE_RESOURCE_GROUP"

        export AZURE_STORAGE_ACCOUNT="${AZURE_STORAGE_ACCOUNT:-composerredhat}"
        rlLogInfo "AZURE_STORAGE_ACCOUNT=$AZURE_STORAGE_ACCOUNT"

        export AZURE_STORAGE_CONTAINER="${AZURE_STORAGE_CONTAINER:-composerredhat}"
        rlLogInfo "AZURE_STORAGE_CONTAINER=$AZURE_STORAGE_CONTAINER"

        if ! rlCheckRpm "python3-pip"; then
            rlRun -t -c "dnf -y install python3-pip"
            rlAssertRpm python3-pip
        fi

        rlRun -t -c "pip3 install ansible[azure]"
    rlPhaseEnd

    rlPhaseStartTest "compose start"
        rlAssertEquals "SELinux operates in enforcing mode" "$(getenforce)" "Enforcing"
        rlRun -t -c "$CLI blueprints push $(dirname $0)/lib/test-http-server.toml"
        UUID=`$CLI compose start test-http-server vhd`
        rlAssertEquals "exit code should be zero" $? 0

        UUID=`echo $UUID | cut -f 2 -d' '`
    rlPhaseEnd

    rlPhaseStartTest "compose finished"
        wait_for_compose $UUID
    rlPhaseEnd

    rlPhaseStartTest "Upload image to Azure"
        rlRun -t -c "$CLI compose image $UUID"
        IMAGE="$UUID-disk.vhd"
        OS_IMAGE_NAME="Composer-$UUID-Automated-Import"

        rlRun -t -c "ansible localhost -m azure_rm_storageblob -a \
                    'resource_group=$AZURE_RESOURCE_GROUP \
                     storage_account_name=$AZURE_STORAGE_ACCOUNT \
                     container=$AZURE_STORAGE_CONTAINER \
                     blob=$IMAGE src=$IMAGE blob_type=page'"

        # create image from blob
        rlRun -t -c "ansible localhost -m azure_rm_image -a \
                    'resource_group=$AZURE_RESOURCE_GROUP name=$OS_IMAGE_NAME os_type=Linux location=eastus \
                    source=https://$AZURE_STORAGE_ACCOUNT.blob.core.windows.net/$AZURE_STORAGE_CONTAINER/$IMAGE'"
    rlPhaseEnd

    rlPhaseStartTest "Start VM instance"
        VM_NAME="Composer-Auto-VM-$UUID"

        SSH_KEY_DIR=`mktemp -d /tmp/composer-ssh-keys.XXXXXX`
        rlRun -t -c "ssh-keygen -t rsa -N '' -f $SSH_KEY_DIR/id_rsa"
        SSH_PUB_KEY=`cat $SSH_KEY_DIR/id_rsa.pub`

        now=$(date -u '+%FT%T')

        TMP_DIR=`mktemp -d /tmp/composer-azure.XXXXX`
        cat > $TMP_DIR/azure-playbook.yaml << __EOF__
---
- hosts: localhost
  connection: local
  tasks:
    - name: Create a VM
      azure_rm_virtualmachine:
        resource_group: $AZURE_RESOURCE_GROUP
        name: $VM_NAME
        vm_size: Standard_B2s
        location: eastus
        admin_username: azure-user
        ssh_password_enabled: false
        ssh_public_keys:
          - path: /home/azure-user/.ssh/authorized_keys
            key_data: "$SSH_PUB_KEY"
        image:
          name: $OS_IMAGE_NAME
          resource_group: $AZURE_RESOURCE_GROUP
        tags:
          "first_seen": "$now"
        storage_account_name: $AZURE_STORAGE_ACCOUNT
__EOF__

        rlRun -t -c "ansible-playbook $TMP_DIR/azure-playbook.yaml"

        response=`ansible localhost -m azure_rm_virtualmachine -a  "resource_group=$AZURE_RESOURCE_GROUP name=$VM_NAME"`
        rlAssert0 "Received VM info successfully" $?
        rlLogInfo "$response"

        IP_ADDRESS=`echo "$response" | grep '"ipAddress":' | cut -f4 -d'"'`
        rlLogInfo "Running instance IP_ADDRESS=$IP_ADDRESS"

        rlLogInfo "Waiting 60sec for instance to initialize ..."
        sleep 60
    rlPhaseEnd

    rlPhaseStartTest "Verify VM instance"
        # run generic tests to verify the instance and check if cloud-init is installed and running
        verify_image azure-user "$IP_ADDRESS" "-i $SSH_KEY_DIR/id_rsa"
        rlRun -t -c "ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i $SSH_KEY_DIR/id_rsa azure-user@$IP_ADDRESS 'rpm -q cloud-init'"
        rlRun -t -c "ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i $SSH_KEY_DIR/id_rsa azure-user@$IP_ADDRESS 'systemctl status cloud-init'"
    rlPhaseEnd

    rlPhaseStartCleanup
        rlRun -t -c "ansible localhost -m azure_rm_virtualmachine -a 'resource_group=$AZURE_RESOURCE_GROUP name=$VM_NAME location=eastus state=absent'"
        rlRun -t -c "ansible localhost -m azure_rm_image -a 'resource_group=$AZURE_RESOURCE_GROUP name=$OS_IMAGE_NAME state=absent'"
        rlRun -t -c "ansible localhost -m azure_rm_storageblob -a 'resource_group=$AZURE_RESOURCE_GROUP storage_account_name=$AZURE_STORAGE_ACCOUNT container=$AZURE_STORAGE_CONTAINER blob=$IMAGE state=absent'"
        rlRun -t -c "$CLI compose delete $UUID"
        rlRun -t -c "rm -rf $IMAGE $SSH_KEY_DIR $TMP_DIR"
    rlPhaseEnd

rlJournalEnd
rlJournalPrintText
