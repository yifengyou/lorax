# Minimal install for containers
# NOTE: This example is for creating a tar, eg.
# livemedia-creator --project RHEL --releasever 8 --make-tar --ks=rhel-container.ks --no-virt
#
# Use network installation
url --url="http://URL-TO-BASEOS/"
repo --name=appstream --baseurl="http://URL-TO-APPSTREAM/"
# Network information
network  --bootproto=dhcp --device=link --activate

# Root password
rootpw --plaintext removethispw
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
# System timezone
timezone  US/Eastern
# System bootloader configuration
bootloader --disabled
# Partition clearing information
clearpart --all --initlabel
# Disk partitioning information
part / --fstype="ext4" --size=4000

%post
# Remove random-seed
rm /var/lib/systemd/random-seed

# Clear /etc/machine-id
rm /etc/machine-id
touch /etc/machine-id
%end

%packages --nocore --instLangs en
httpd
-kernel
policycoreutils
%end
