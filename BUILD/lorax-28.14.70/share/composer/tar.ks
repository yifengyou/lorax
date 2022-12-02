# Lorax Composer tar output kickstart template

# Firewall configuration
firewall --enabled

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
# System bootloader configuration (tar doesn't need a bootloader)
bootloader --location=none

%post
# Remove random-seed
rm /var/lib/systemd/random-seed

# Clear /etc/machine-id
rm /etc/machine-id
touch /etc/machine-id

# Remove the rescue kernel and image to save space
rm -f /boot/*-rescue*
%end

# NOTE Do NOT add any other sections after %packages
%packages --nocore
# Packages requires to support this output format go here
policycoreutils
selinux-policy-targeted

# NOTE lorax-composer will add the blueprint packages below here, including the final %end
