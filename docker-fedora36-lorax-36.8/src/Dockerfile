FROM fedora:36
LABEL maintainers="yifengyou 13518106436@163.com"

RUN cat /etc/yum.repos.d/*.repo && dnf makecache && \
    dnf -y install systemd findutils which passwd

RUN dnf install -y \
        createrepo \
        git \
        openssh-server \
        vim \
        net-tools \
        iproute \
        tmux \
        dnf-utils

RUN dnf install -y \
    lorax \
    createrepo \
    && dnf clean all

COPY files/lorax-init /usr/bin/lorax-init
