# Copyright (c) 2011 Intel, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os
import stat
import shutil

from mic import kickstart, msger
from mic.utils import fs_related, runner, misc
from mic.utils.partitionedfs import PartitionedMount
from mic.utils.errors import CreatorError, MountError
from mic.imager.baseimager import BaseImageCreator


class RawImageCreator(BaseImageCreator):
    """Installs a system into a file containing a partitioned disk image.

    ApplianceImageCreator is an advanced ImageCreator subclass; a sparse file
    is formatted with a partition table, each partition loopback mounted
    and the system installed into an virtual disk. The disk image can
    subsequently be booted in a virtual machine or accessed with kpartx
    """

    def __init__(self, creatoropts=None, pkgmgr=None, compress_image=None, generate_bmap=None, fstab_entry="uuid"):
        """Initialize a ApplianceImageCreator instance.

            This method takes the same arguments as ImageCreator.__init__()
        """
        BaseImageCreator.__init__(self, creatoropts, pkgmgr)

        self.__instloop = None
        self.__imgdir = None
        self.__disks = {}
        self.__disk_format = "raw"
        self._disk_names = []
        self._ptable_format = self.ks.handler.bootloader.ptable
        self.vmem = 512
        self.vcpu = 1
        self.checksum = False
        self.use_uuid = fstab_entry == "uuid"
        self.appliance_version = None
        self.appliance_release = None
        self.compress_image = compress_image
        self.bmap_needed = generate_bmap
        #self.getsource = False
        #self.listpkg = False

        self._dep_checks.extend(["sync", "kpartx", "parted", "extlinux"])

    def configure(self, repodata = None):
        import subprocess
        def chroot():
            os.chroot(self._instroot)
            os.chdir("/")

        if os.path.exists(self._instroot + "/usr/bin/Xorg"):
            subprocess.call(["/bin/chmod", "u+s", "/usr/bin/Xorg"],
                            preexec_fn = chroot)

        BaseImageCreator.configure(self, repodata)

    def _get_fstab(self):
        s = ""
        for mp in self.__instloop.mountOrder:
            p = None
            for p1 in self.__instloop.partitions:
                if p1['mountpoint'] == mp:
                    p = p1
                    break

            if self.use_uuid and p['uuid']:
                device = "UUID=%s" % p['uuid']
            else:
                device = "/dev/%s%-d" % (p['disk_name'], p['num'])

            s += "%(device)s  %(mountpoint)s  %(fstype)s  %(fsopts)s 0 0\n" % {
               'device': device,
               'mountpoint': p['mountpoint'],
               'fstype': p['fstype'],
               'fsopts': "defaults,noatime" if not p['fsopts'] else p['fsopts']}

            if p['mountpoint'] == "/":
                for subvol in self.__instloop.subvolumes:
                    if subvol['mountpoint'] == "/":
                        continue
                    s += "%(device)s  %(mountpoint)s  %(fstype)s  %(fsopts)s 0 0\n" % {
                         'device': "/dev/%s%-d" % (p['disk_name'], p['num']),
                         'mountpoint': subvol['mountpoint'],
                         'fstype': p['fstype'],
                         'fsopts': "defaults,noatime" if not subvol['fsopts'] else subvol['fsopts']}

        s += "devpts     /dev/pts  devpts  gid=5,mode=620   0 0\n"
        s += "tmpfs      /dev/shm  tmpfs   defaults         0 0\n"
        s += "proc       /proc     proc    defaults         0 0\n"
        s += "sysfs      /sys      sysfs   defaults         0 0\n"
        return s

    def _create_mkinitrd_config(self):
        """write to tell which modules to be included in initrd"""

        mkinitrd = ""
        mkinitrd += "PROBE=\"no\"\n"
        mkinitrd += "MODULES+=\"ext3 ata_piix sd_mod libata scsi_mod\"\n"
        mkinitrd += "rootfs=\"ext3\"\n"
        mkinitrd += "rootopts=\"defaults\"\n"

        msger.debug("Writing mkinitrd config %s/etc/sysconfig/mkinitrd" \
                    % self._instroot)
        os.makedirs(self._instroot + "/etc/sysconfig/",mode=644)
        cfg = open(self._instroot + "/etc/sysconfig/mkinitrd", "w")
        cfg.write(mkinitrd)
        cfg.close()

    def _get_parts(self):
        if not self.ks:
            raise CreatorError("Failed to get partition info, "
                               "please check your kickstart setting.")

        # Set a default partition if no partition is given out
        if not self.ks.handler.partition.partitions:
            partstr = "part / --size 1900 --ondisk sda --fstype=ext3"
            args = partstr.split()
            pd = self.ks.handler.partition.parse(args[1:])
            if pd not in self.ks.handler.partition.partitions:
                self.ks.handler.partition.partitions.append(pd)

        # partitions list from kickstart file
        return kickstart.get_partitions(self.ks)

    def get_disk_names(self):
        """ Returns a list of physical target disk names (e.g., 'sdb') which
        will be created. """

        if self._disk_names:
            return self._disk_names

        #get partition info from ks handler
        parts = self._get_parts()

        for i in range(len(parts)):
            if parts[i].disk:
                disk_name = parts[i].disk
            else:
                raise CreatorError("Failed to create disks, no --ondisk "
                                   "specified in partition line of ks file")

            if parts[i].mountpoint and not parts[i].fstype:
                raise CreatorError("Failed to create disks, no --fstype "
                                    "specified for partition with mountpoint "
                                    "'%s' in the ks file")

            self._disk_names.append(disk_name)

        return self._disk_names

    def _full_name(self, name, extention):
        """ Construct full file name for a file we generate. """
        return "%s-%s.%s" % (self.name, name, extention)

    def _full_path(self, path, name, extention):
        """ Construct full file path to a file we generate. """
        return os.path.join(path, self._full_name(name, extention))

    #
    # Actual implemention
    #
    def _mount_instroot(self, base_on = None):
        parts = self._get_parts()
        self.__instloop = PartitionedMount(self._instroot)

        for p in parts:
            self.__instloop.add_partition(int(p.size),
                                          p.disk,
                                          p.mountpoint,
                                          p.fstype,
                                          p.label,
                                          fsopts = p.fsopts,
                                          boot = p.active,
                                          align = p.align,
                                          part_type = p.part_type)

        self.__instloop.layout_partitions(self._ptable_format)

        # Create the disks
        self.__imgdir = self._mkdtemp()
        for disk_name, disk in self.__instloop.disks.items():
            full_path = self._full_path(self.__imgdir, disk_name, "raw")
            msger.debug("Adding disk %s as %s with size %s bytes" \
                        % (disk_name, full_path, disk['min_size']))

            disk_obj = fs_related.SparseLoopbackDisk(full_path,
                                                     disk['min_size'])
            self.__disks[disk_name] = disk_obj
            self.__instloop.add_disk(disk_name, disk_obj)

        self.__instloop.mount()
        self._create_mkinitrd_config()

    def _get_required_packages(self):
        required_packages = BaseImageCreator._get_required_packages(self)
        if not self.target_arch or not self.target_arch.startswith("arm"):
            required_packages += ["syslinux", "syslinux-extlinux"]
        return required_packages

    def _get_excluded_packages(self):
        return BaseImageCreator._get_excluded_packages(self)

    def _get_syslinux_boot_config(self):
        rootdev = None
        root_part_uuid = None
        for p in self.__instloop.partitions:
            if p['mountpoint'] == "/":
                rootdev = "/dev/%s%-d" % (p['disk_name'], p['num'])
                root_part_uuid = p['partuuid']

        return (rootdev, root_part_uuid)

    def _create_syslinux_config(self):

        splash = os.path.join(self._instroot, "boot/extlinux")
        if os.path.exists(splash):
            splashline = "menu background splash.jpg"
        else:
            splashline = ""

        (rootdev, root_part_uuid) = self._get_syslinux_boot_config()
        options = self.ks.handler.bootloader.appendLine

        #XXX don't hardcode default kernel - see livecd code
        syslinux_conf = ""
        syslinux_conf += "prompt 0\n"
        syslinux_conf += "timeout 1\n"
        syslinux_conf += "\n"
        syslinux_conf += "default vesamenu.c32\n"
        syslinux_conf += "menu autoboot Starting %s...\n" % self.distro_name
        syslinux_conf += "menu hidden\n"
        syslinux_conf += "\n"
        syslinux_conf += "%s\n" % splashline
        syslinux_conf += "menu title Welcome to %s!\n" % self.distro_name
        syslinux_conf += "menu color border 0 #ffffffff #00000000\n"
        syslinux_conf += "menu color sel 7 #ffffffff #ff000000\n"
        syslinux_conf += "menu color title 0 #ffffffff #00000000\n"
        syslinux_conf += "menu color tabmsg 0 #ffffffff #00000000\n"
        syslinux_conf += "menu color unsel 0 #ffffffff #00000000\n"
        syslinux_conf += "menu color hotsel 0 #ff000000 #ffffffff\n"
        syslinux_conf += "menu color hotkey 7 #ffffffff #ff000000\n"
        syslinux_conf += "menu color timeout_msg 0 #ffffffff #00000000\n"
        syslinux_conf += "menu color timeout 0 #ffffffff #00000000\n"
        syslinux_conf += "menu color cmdline 0 #ffffffff #00000000\n"

        versions = []
        kernels = self._get_kernel_versions()
        symkern = "%s/boot/vmlinuz" % self._instroot

        if os.path.lexists(symkern):
            v = os.path.realpath(symkern).replace('%s-' % symkern, "")
            syslinux_conf += "label %s\n" % self.distro_name.lower()
            syslinux_conf += "\tmenu label %s (%s)\n" % (self.distro_name, v)
            syslinux_conf += "\tlinux ../vmlinuz\n"
            if self._ptable_format == 'msdos':
                rootstr = rootdev
            else:
                if not root_part_uuid:
                    raise MountError("Cannot find the root GPT partition UUID")
                rootstr = "PARTUUID=%s" % root_part_uuid
            syslinux_conf += "\tappend ro root=%s %s\n" % (rootstr, options)
            syslinux_conf += "\tmenu default\n"
        else:
            for kernel in kernels:
                for version in kernels[kernel]:
                    versions.append(version)

            footlabel = 0
            for v in versions:
                syslinux_conf += "label %s%d\n" \
                                 % (self.distro_name.lower(), footlabel)
                syslinux_conf += "\tmenu label %s (%s)\n" % (self.distro_name, v)
                syslinux_conf += "\tlinux ../vmlinuz-%s\n" % v
                syslinux_conf += "\tappend ro root=%s %s\n" \
                                 % (rootdev, options)
                if footlabel == 0:
                    syslinux_conf += "\tmenu default\n"
                footlabel += 1;

        msger.debug("Writing syslinux config %s/boot/extlinux/extlinux.conf" \
                    % self._instroot)
        cfg = open(self._instroot + "/boot/extlinux/extlinux.conf", "w")
        cfg.write(syslinux_conf)
        cfg.close()

    def _install_syslinux(self):
        for name in self.__disks.keys():
            loopdev = self.__disks[name].device

            # Set MBR
            mbrfile = "%s/usr/share/syslinux/" % self._instroot
            if self._ptable_format == 'gpt':
                mbrfile += "gptmbr.bin"
            else:
                mbrfile += "mbr.bin"

            msger.debug("Installing syslinux bootloader '%s' to %s" % \
                        (mbrfile, loopdev))

            mbrsize = os.stat(mbrfile)[stat.ST_SIZE]
            rc = runner.show(['dd', 'if=%s' % mbrfile, 'of=' + loopdev])
            if rc != 0:
                raise MountError("Unable to set MBR to %s" % loopdev)


            # Ensure all data is flushed to disk before doing syslinux install
            runner.quiet('sync')

            fullpathsyslinux = fs_related.find_binary_path("extlinux")
            rc = runner.show([fullpathsyslinux,
                              "-i",
                              "%s/boot/extlinux" % self._instroot])
            if rc != 0:
                raise MountError("Unable to install syslinux bootloader to %s" \
                                 % loopdev)

    def _create_bootconfig(self):
        #If syslinux is available do the required configurations.
        if os.path.exists("%s/usr/share/syslinux/" % (self._instroot)) \
           and os.path.exists("%s/boot/extlinux/" % (self._instroot)):
            self._create_syslinux_config()
            self._install_syslinux()

    def _unmount_instroot(self):
        if not self.__instloop is None:
            try:
                self.__instloop.cleanup()
            except MountError, err:
                msger.warning("%s" % err)

    def _resparse(self, size = None):
        return self.__instloop.resparse(size)

    def _stage_final_image(self):
        """Stage the final system image in _outdir.
           write meta data
        """
        self._resparse()

        if self.compress_image:
            for imgfile in os.listdir(self.__imgdir):
                if imgfile.endswith('.raw') or imgfile.endswith('bin'):
                    imgpath = os.path.join(self.__imgdir, imgfile)
                    misc.compressing(imgpath, self.compress_image)

        if self.pack_to:
            dst = os.path.join(self._outdir, self.pack_to)
            msger.info("Pack all raw images to %s" % dst)
            misc.packing(dst, self.__imgdir)
        else:
            msger.debug("moving disks to stage location")
            for imgfile in os.listdir(self.__imgdir):
                src = os.path.join(self.__imgdir, imgfile)
                dst = os.path.join(self._outdir, imgfile)
                msger.debug("moving %s to %s" % (src,dst))
                shutil.move(src,dst)
        self._write_image_xml()

    def _write_image_xml(self):
        imgarch = "i686"
        if self.target_arch and self.target_arch.startswith("arm"):
            imgarch = "arm"
        xml = "<image>\n"

        name_attributes = ""
        if self.appliance_version:
            name_attributes += " version='%s'" % self.appliance_version
        if self.appliance_release:
            name_attributes += " release='%s'" % self.appliance_release
        xml += "  <name%s>%s</name>\n" % (name_attributes, self.name)
        xml += "  <domain>\n"
        # XXX don't hardcode - determine based on the kernel we installed for
        # grub baremetal vs xen
        xml += "    <boot type='hvm'>\n"
        xml += "      <guest>\n"
        xml += "        <arch>%s</arch>\n" % imgarch
        xml += "      </guest>\n"
        xml += "      <os>\n"
        xml += "        <loader dev='hd'/>\n"
        xml += "      </os>\n"

        i = 0
        for name in self.__disks.keys():
            full_name = self._full_name(name, self.__disk_format)
            xml += "      <drive disk='%s' target='hd%s'/>\n" \
                       % (full_name, chr(ord('a') + i))
            i = i + 1

        xml += "    </boot>\n"
        xml += "    <devices>\n"
        xml += "      <vcpu>%s</vcpu>\n" % self.vcpu
        xml += "      <memory>%d</memory>\n" %(self.vmem * 1024)
        for network in self.ks.handler.network.network:
            xml += "      <interface/>\n"
        xml += "      <graphics/>\n"
        xml += "    </devices>\n"
        xml += "  </domain>\n"
        xml += "  <storage>\n"

        if self.checksum is True:
            for name in self.__disks.keys():
                diskpath = self._full_path(self._outdir, name, \
                                           self.__disk_format)
                full_name = self._full_name(name, self.__disk_format)

                msger.debug("Generating disk signature for %s" % full_name)

                xml += "    <disk file='%s' use='system' format='%s'>\n" \
                       % (full_name, self.__disk_format)

                hashes = misc.calc_hashes(diskpath, ('sha1', 'sha256'))

                xml +=  "      <checksum type='sha1'>%s</checksum>\n" \
                        % hashes[0]
                xml += "      <checksum type='sha256'>%s</checksum>\n" \
                       % hashes[1]
                xml += "    </disk>\n"
        else:
            for name in self.__disks.keys():
                full_name = self._full_name(name, self.__disk_format)
                xml += "    <disk file='%s' use='system' format='%s'/>\n" \
                       % (full_name, self.__disk_format)

        xml += "  </storage>\n"
        xml += "</image>\n"

        msger.debug("writing image XML to %s/%s.xml" %(self._outdir, self.name))
        cfg = open("%s/%s.xml" % (self._outdir, self.name), "w")
        cfg.write(xml)
        cfg.close()

    def generate_bmap(self):
        """ Generate block map file for the image. The idea is that while disk
        images we generate may be large (e.g., 4GiB), they may actually contain
        only little real data, e.g., 512MiB. This data are files, directories,
        file-system meta-data, partition table, etc. In other words, when
        flashing the image to the target device, you do not have to copy all the
        4GiB of data, you can copy only 512MiB of it, which is 4 times faster.

        This function generates the block map file for an arbitrary image that
        mic has generated. The block map file is basically an XML file which
        contains a list of blocks which have to be copied to the target device.
        The other blocks are not used and there is no need to copy them. """

        if self.bmap_needed is None:
            return

        from mic.utils import BmapCreate
        msger.info("Generating the map file(s)")

        for name in self.__disks.keys():
            image = self._full_path(self.__imgdir, name, self.__disk_format)
            bmap_file = self._full_path(self._outdir, name, "bmap")

            msger.debug("Generating block map file '%s'" % bmap_file)

            try:
                creator = BmapCreate.BmapCreate(image, bmap_file)
                creator.generate()
                del creator
            except BmapCreate.Error as err:
                raise CreatorError("Failed to create bmap file: %s" % str(err))
