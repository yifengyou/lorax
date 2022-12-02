# Lorax Composer openstack output kickstart template

# Firewall configuration
firewall --disabled

# NOTE: The root account is locked by default
# Network information
network  --bootproto=dhcp --onboot=on --activate
# NOTE: keyboard and lang can be replaced by blueprint customizations.locale settings
# System keyboard
keyboard --xlayouts=us --vckeymap=us
# System language
lang en_US.UTF-8
# SELinux configuration
selinux --enforcing
# Installation logging level
logging --level=info
# Shutdown after installation
shutdown
# System bootloader configuration
bootloader --location=mbr --append="no_timer_check console=ttyS0,115200n8 console=tty1 net.ifnames=0"
# Add platform specific partitions
reqpart --add-boot

# Start sshd and cloud-init at boot time
services --enabled=sshd,cloud-init,cloud-init-local,cloud-config,cloud-final

%post
# Remove random-seed
rm /var/lib/systemd/random-seed

# Clear /etc/machine-id
rm /etc/machine-id
touch /etc/machine-id

# Remove the rescue kernel and image to save space
rm -f /boot/*-rescue*
%end

%packages
kernel
selinux-policy-targeted

# Make sure virt guest agents are installed
qemu-guest-agent
spice-vdagent
cloud-init

# NOTE lorax-composer will add the recipe packages below here, including the final %end
