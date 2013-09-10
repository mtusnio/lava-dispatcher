# Copyright (C) 2012 Linaro Limited
#
# Author: Michael Hudson-Doyle <michael.hudson@linaro.org>
# Author: Nicholas Schutt <nick.schutt@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

import contextlib
import logging
import os
import time

from lava_dispatcher.device.master import (
    MasterCommandRunner,
)
from lava_dispatcher.device.target import (
    Target
)
from lava_dispatcher.errors import (
    CriticalError,
)
from lava_dispatcher.downloader import (
    download_image,
    download_with_retry,
)
from lava_dispatcher.utils import (
    mk_targz,
    rmtree,
)
from lava_dispatcher.client.lmc_utils import (
    generate_image,
)
from lava_dispatcher.ipmi import IpmiPxeBoot


class IpmiPxeTarget(Target):

    MASTER_PS1 = 'root@master [rc=$(echo \$?)]# '
    MASTER_PS1_PATTERN = 'root@master \[rc=(\d+)\]# '

    def __init__(self, context, config):
        super(IpmiPxeTarget, self).__init__(context, config)
        self.proc = self.context.spawn(self.config.connection_command, timeout=1200)
        self.device_version = None
        if self.config.ecmeip is None:
            msg = "The ecmeip address is not set for this target"
            logging.error(msg)
            raise CriticalError(msg)
        self.bootcontrol = IpmiPxeBoot(context, self.config.ecmeip)

    def get_device_version(self):
        return self.device_version

    def power_on(self):
        self.bootcontrol.power_on_boot_image()
        return self.proc

    def power_off(self, proc):
        pass

    def deploy_linaro(self, hwpack, rfs, bootloadertype):
        image_file = generate_image(self, hwpack, rfs, self.scratch_dir, bootloadertype,
                                    extra_boot_args='1', image_size='1G')
        self._customize_linux(image_file)
        self._deploy_image(image_file, '/dev/sda')

    def deploy_linaro_prebuilt(self, image, bootloadertype):
        image_file = download_image(image, self.context, self.scratch_dir)
        self._customize_linux(image_file)
        self._deploy_image(image_file, '/dev/sda')

    def _deploy_image(self, image_file, device):
        with self._as_master() as runner:

            # erase the first part of the disk to make sure the new deploy works
            runner.run("dd if=/dev/zero of=%s bs=4M count=4" % device, timeout=1800)

            # compress the image to reduce the transfer size
            if not image_file.endswith('.bz2') and not image_file.endswith('gz'):
                os.system('bzip2 -9v ' + image_file)
                image_file += '.bz2'

            tmpdir = self.context.config.lava_image_tmpdir
            url = self.context.config.lava_image_url
            image_file = image_file.replace(tmpdir, '')
            image_url = '/'.join(u.strip('/') for u in [url, image_file])

            build_dir = '/builddir'
            image_file_base = build_dir + '/' + '/'.join(image_file.split('/')[-1:])

            decompression_cmd = None
            if image_file_base.endswith('.gz'):
                decompression_cmd = '/bin/gzip -dc'
            elif image_file_base.endswith('.bz2'):
                decompression_cmd = '/bin/bzip2 -dc'

            runner.run('mkdir %s' % build_dir)
            runner.run('mount -t tmpfs -o size=100%% tmpfs %s' % build_dir)
            runner.run('wget -O %s %s' % (image_file_base, image_url), timeout=1800)

            if decompression_cmd is not None:
                cmd = '%s %s | dd bs=4M of=%s' % (decompression_cmd, image_file_base, device)
            else:
                cmd = 'dd bs=4M if=%s of=%s' % (image_file_base, device)

            runner.run(cmd, timeout=1800)
            runner.run('umount %s' % build_dir)

            self.resize_rootfs_partition(runner)

    def get_partition(self, runner, partition):
        if partition == self.config.boot_part:
            partition = '/dev/disk/by-label/boot'
        elif partition == self.config.root_part:
            partition = '/dev/disk/by-label/rootfs'
        else:
            raise RuntimeError(
                'unknown master image partition(%d)' % partition)
        return partition

    def resize_rootfs_partition(self, runner):
        partno = self.config.root_part
        start = None

        runner.run('parted -s /dev/sda print',
                   response='\s+%s\s+([0-9.]+.B)\s+\S+\s+\S+\s+primary\s+(\S+)' % partno,
                   wait_prompt=False)
        if runner.match_id != 0:
            msg = "Unable to determine rootfs partition"
            logging.warning(msg)
        else:
            start = runner.match.group(1)
            parttype = runner.match.group(2)

            if parttype == 'ext2' or parttype == 'ext3' or parttype == 'ext4':
                runner.run('parted -s /dev/sda rm %s' % partno)
                runner.run('parted -s /dev/sda mkpart primary %s 100%%' % start)
                runner.run('resize2fs -f /dev/sda%s' % partno)
            elif parttype == 'brtfs':
                logging.warning("resize of btrfs partition not supported")
            else:
                logging.warning("unknown partition type for resize: %s" % parttype)

    @contextlib.contextmanager
    def file_system(self, partition, directory):
        logging.info('attempting to access master filesystem %r:%s' %
                     (partition, directory))

        assert directory != '/', "cannot mount entire partition"

        with self._as_master() as runner:
            runner.run('mkdir -p /mnt')
            partition = self.get_partition(runner, partition)
            runner.run('mount %s /mnt' % partition)
            try:
                targetdir = '/mnt/%s' % directory
                runner.run('mkdir -p %s' % targetdir)

                parent_dir, target_name = os.path.split(targetdir)

                runner.run('/bin/tar -cmzf /tmp/fs.tgz -C %s %s' % (parent_dir, target_name))
                runner.run('cd /tmp')  # need to be in same dir as fs.tgz

                ip = runner.get_target_ip()
                url_base = self._start_busybox_http_server(runner, ip)

                url = url_base + '/fs.tgz'
                logging.info("Fetching url: %s" % url)
                tf = download_with_retry(self.context, self.scratch_dir, url, False)

                tfdir = os.path.join(self.scratch_dir, str(time.time()))

                try:
                    os.mkdir(tfdir)
                    self.context.run_command('/bin/tar -C %s -xzf %s' % (tfdir, tf))
                    yield os.path.join(tfdir, target_name)

                finally:
                    tf = os.path.join(self.scratch_dir, 'fs.tgz')
                    mk_targz(tf, tfdir)
                    rmtree(tfdir)

                    # get the last 2 parts of tf, ie "scratchdir/tf.tgz"
                    tf = '/'.join(tf.split('/')[-2:])
                    runner.run('rm -rf %s' % targetdir)
                    self._target_extract(runner, tf, parent_dir)

            finally:
                    self._stop_busybox_http_server(runner)
                    runner.run('umount /mnt')

    @contextlib.contextmanager
    def _as_master(self):
        self.bootcontrol.power_on_boot_master()
        self.proc.expect("\(initramfs\)")
        self.proc.sendline('export PS1="%s"' % self.MASTER_PS1)
        self.proc.expect(self.MASTER_PS1_PATTERN, timeout=180, lava_no_logging=1)
        runner = MasterCommandRunner(self)

        runner.run(". /scripts/functions")
        runner.run("DEVICE=%s configure_networking" %
                   self.config.default_network_interface)

        # we call dhclient even though configure_networking above already
        # picked up a IP address. configure_networking brings the interface up,
        # but does not configure DNS properly. dhclient needs the interface to
        # be up, and will set DNS correctly. In the end we are querying DHCP
        # twice, but with a properly configured DHCP server (i.e. one that will
        # give the same address for a given MAC address), this should not be a
        # problem.
        runner.run("mkdir -p /var/run")
        runner.run("mkdir -p /var/lib/dhcp")
        runner.run("dhclient -v -1")

        self.device_version = runner.get_device_version()

        try:
            yield runner
        finally:
            logging.debug("deploy done")


target_class = IpmiPxeTarget
