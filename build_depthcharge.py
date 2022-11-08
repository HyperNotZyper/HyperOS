#!/usr/bin/env python3
# This script is cloud oriented, so it is not very user-friendly.

import sys
import os
import argparse
from typing import Tuple

from functions import *


# parse arguments from the cli. Only for testing/advanced use. All other parameters are handled by cli_input.py
def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true", dest="verbose", default=False,
                        help="Print more output")
    parser.add_argument("--dev", action="store_true", dest="dev_build", default=False,
                        help="Use latest dev build. May be unstable.")
    parser.add_argument("--alt", action="store_true", dest="alt", default=False,
                        help="Use alt kernel. Only for older devices.")
    parser.add_argument("--exp", action="store_true", dest="exp", default=False,
                        help="Use experimental 5.15 kernel.")
    parser.add_argument("--mainline", action="store_true", dest="mainline", default=False,
                        help="Use mainline kernel instead of modified chromeos kernel.")
    return parser.parse_args()


# Create, mount, partition the img and flash the eupnea's kernel
def prepare_image() -> Tuple[str, str]:
    print_status("Preparing image")

    bash(f"fallocate -l 8G HyperOS.bin")
    print_status("Mounting empty image")
    img_mnt = bash("losetup -f --show HyperOS.bin")
    if img_mnt == "":
        print_error("Failed to mount image")
        exit(1)

    # partition image
    print_status("Preparing device/image partition")

    # format image as per depthcharge requirements
    # READ: https://wiki.gentoo.org/wiki/Creating_bootable_media_for_depthcharge_based_devices
    bash(f"parted -s {img_mnt} mklabel gpt")
    bash(f"parted -s -a optimal {img_mnt} unit mib mkpart Kernel 1 65")  # kernel partition
    bash(f"parted -s -a optimal {img_mnt} unit mib mkpart Root 65 100%")  # rootfs partition
    bash(f"cgpt add -i 1 -t kernel -S 1 -T 5 -P 15 {img_mnt}")  # depthcharge flags

    print_status("Formatting rootfs part")
    rootfs_mnt = img_mnt + "p2"  # second partition is rootfs
    # Create rootfs ext4 partition
    bash(f"yes 2>/dev/null | mkfs.ext4 {rootfs_mnt}")  # 2>/dev/null is to supress yes broken pipe warning
    # Mount rootfs partition
    bash(f"mount {rootfs_mnt} /mnt/hyperos")

    # get uuid of rootfs partition
    rootfs_partuuid = bash(f"blkid -o value -s PARTUUID {rootfs_mnt}")
    # write PARTUUID to kernel flags and save it as a file
    with open(f"configs/kernel.flags", "r") as flags:
        temp_cmdline = flags.read().replace("insert_partuuid", rootfs_partuuid).strip()
    # SELinux is temporarily disabled, until we can figure out how to relabel files without rebooting
    with open("kernel.flags", "w") as config:
        config.write(temp_cmdline)

    print_status("Partitioning complete")
    flash_kernel(f"{img_mnt}p1")
    return rootfs_partuuid, img_mnt


def flash_kernel(kernel_part: str) -> None:
    print_status("Flashing kernel to device/image")
    # Sign kernel
    bash("futility vbutil_kernel --arch x86_64 --version 1 --keyblock /usr/share/vboot/devkeys/kernel.keyblock"
         + " --signprivate /usr/share/vboot/devkeys/kernel_data_key.vbprivk --bootloader kernel.flags" +
         " --config kernel.flags --vmlinuz /tmp/hyperos/bzImage --pack /tmp/hyperos/bzImage.signed")
    bash(f"dd if=/tmp/hyperos/bzImage.signed of={kernel_part}")  # part 1 is the kernel partition

    print_status("Kernel flashed successfully")


# Make a bootable rootfs
def bootstrap_rootfs(root_partuuid) -> None:

    with open("/mnt/hyperos/etc/apt/sources.list", "a") as file:
        file.write(f"\ndeb http://archive.ubuntu.com/ubuntu kinetic 22.10-backports main "
                   "restricted universe multiverse\n")
        file.write(f"\ndeb http://security.ubuntu.com/ubuntu kinetic 22.10-security main"
                   f" restricted universe multiverse\n")
        file.write(f"\ndeb http://archive.ubuntu.com/ubuntu kinetic 22.10-updates main "
                   f"restricted universe multiverse\n")

        print_status("Installing dependencies")
    chroot("apt-get update -y")
    chroot("apt-get install -y linux-firmware network-manager software-properties-common")
    chroot("apt-get install -y git cgpt vboot-kernel-utils cloud-utils rsync")  # postinstall dependencies
    chroot("apt-get remove -y xserver-xorg-input-synaptics")
    chroot("apt-get install -y xserver-xorg-input-libinput")
    chroot("apt-get install -y Firefox")
    
def configure_rootfs() -> None:
    # Extract kernel modules
    print_status("Extracting kernel modules")
    rmdir("/mnt/hyperos/lib/modules")  # remove all old modules
    mkdir("/mnt/hyperos/lib/modules")
    bash(f"tar xpf /tmp/hyperos/modules.tar.xz -C /mnt/hyperos/lib/modules/ --checkpoint=.10000")
    print("")  # break line after tar

    # Enable loading modules needed for hyperos
    cpfile("configs/eupnea-modules.conf", "/mnt/hyperos/etc/modules-load.d/eupnea-modules.conf")

    # Extract kernel headers
    print_status("Extracting kernel headers")
    dir_kernel_version = bash(f"ls /mnt/hyperos/lib/modules/").strip()  # get modules dir name
    rmdir(f"/mnt/hyperos/usr/src/linux-headers-{dir_kernel_version}", keep_dir=False)  # remove old headers
    mkdir(f"/mnt/hyperos/usr/src/linux-headers-{dir_kernel_version}", create_parents=True)
    bash(f"tar xpf /tmp/hyperos-build/headers.tar.xz -C /mnt/hyperos/usr/src/linux-headers-{dir_kernel_version}/ "
         f"--checkpoint=.10000")
    print("")  # break line after tar
    chroot(f"ln -s /usr/src/linux-headers-{dir_kernel_version}/ "
           f"/lib/modules/{dir_kernel_version}/build")  # use chroot for correct symlink

    # Copy chromebook firmware
    print_status("Copying google firmware")
    rmdir("/mnt/hyperos/lib/firmware")
    cpdir("linux-firmware", "/mnt/hyperos/lib/firmware")

    # Set device hostname
    with open("/mnt/hyperos/etc/hostname", "w") as hostname_file:
        hostname_file.write("hyperbook" + "\n")

    print_status("Adding User")
    chroot(f"useradd --create-home --shell /bin/bash HyperOSuser")
    chroot(f'echo "HyperOSuser:root" | chpasswd')
    chroot(f"usermod -aG sudo HyperOSuser:")

    print_status("Copying eupnea scripts and configs")
    # Copy postinstall scripts
    for file in Path("postinstall-scripts").iterdir():
        if file.is_file():
            if file.name == "LICENSE" or file.name == "README.md" or file.name == ".gitignore":
                continue  # dont copy license, readme and gitignore
            else:
                cpfile(file.absolute().as_posix(), f"/mnt/hyperos/usr/local/bin/{file.name}")

    # copy audio setup script
    cpfile("audio-scripts/setup-audio", "/mnt/hyperos/usr/local/bin/setup-audio")

    # copy functions file
    cpfile("functions.py", "/mnt/hyperos/usr/local/bin/functions.py")
    chroot("chmod 755 /usr/local/bin/*")  # make scripts executable in system

    # copy configs
    mkdir("/mnt/hyperos/etc/hyperos")
    cpdir("configs", "/mnt/hyperos/etc/hyperos")  # eupnea-builder configs
    cpdir("postinstall-scripts/configs", "/mnt/hyperos/etc/hyperos")  # postinstall configs
    cpdir("audio-scripts/configs", "/mnt/hyperos/etc/hyperos")  # audio configs

    # copy preset eupnea settings file for postinstall scripts to read
    cpfile("configs/eupnea.json", "/mnt/hyperos/etc/eupnea.json")

    # Add postinstall service hook
    print_status("Adding postinstall service")
    cpfile("configs/postinstall.service", "/mnt/hyperos/etc/systemd/system/postinstall.service")
    chroot("systemctl enable postinstall.service")

    print_status("Fixing sleep")
    # disable hibernation aka S4 sleep, READ: https://eupnea-linux.github.io/docs.html#/pages/bootlock
    # TODO: Fix S4 sleep
    mkdir("/mnt/hyperos/etc/systemd/")  # just in case systemd path doesn't exist
    with open("/mnt/hyperos/etc/systemd/sleep.conf", "a") as conf:
        conf.write("SuspendState=freeze\nHibernateState=freeze\n")

    # TODO: Fix failing services
    # The services below fail to start, so they are disabled
    # ssh
    rmfile("/mnt/hyperos/etc/systemd/system/multi-user.target.wants/ssh.service")
    rmfile("/mnt/hyperos/etc/systemd/system/sshd.service")
    # TODO: Fix zram
    chroot("dnf remove zram-generator-defaults -y")  # remove zram as it fails for some reason
    chroot("systemctl disable systemd-zram-setup@zram0.service")  # disable zram service

    # The default fstab file has the wrong PARTUUID -> system boots in emergency mode if not fixed
    # with open("configs/fstab.txt", "r") as f:
    #     fstab = f.read()
    # fstab = fstab.replace("insert_partuuid", root_partuuid)
    # with open("/mnt/eupnea/etc/fstab", "w") as f:
    #     f.write(fstab)


def customize_kde() -> None:
    # Install KDE
    chroot("DEBIAN_FRONTEND=noninteractive apt-get install -y kde-standard")
    # Set system to boot to gui
    chroot("systemctl set-default graphical.target")
    # Add chromebook keyboard layout. Needs to be done after install Xorg
    print_status("Backing up default keymap and setting Chromebook layout")
    cpfile("/mnt/hyperos/usr/share/X11/xkb/symbols/pc", "/mnt/hyperos/usr/share/X11/xkb/symbols/pc.default")
    cpfile("configs/xkb/xkb.chromebook", "/mnt/hyperos/usr/share/X11/xkb/symbols/pc")

    # Set kde ui settings
    print_status("Setting General UI settings")
    mkdir("/mnt/hyperos/home/liveuser/.config")
    cpfile("configs/kde-configs/kwinrc", "/mnt/hyperos/home/liveuser/.config/kwinrc")  # set general kwin settings
    cpfile("configs/kde-configs/kcminputrc", "/mnt/hyperos/home/liveuser/.config/kcminputrc")  # set touchpad settings
    chroot("chown -R HyperOSuser:HyperOSuser /home/HyperOSuser/.config")  # set permissions


def compress_image(img_mnt: str) -> None:
    print_status("Shrinking image")

    # Shrink image to actual size
    bash(f"e2fsck -fpv {img_mnt}p2")  # Force check filesystem for errors
    bash(f"resize2fs -M {img_mnt}p2")
    block_count = int(bash(f"dumpe2fs -h {img_mnt}p2 | grep 'Block count:'")[12:].split()[0])
    actual_fs_in_bytes = block_count * 4096
    # the kernel part is always the same size -> sector amount: 131072 * 512 => 67108864 bytes
    actual_fs_in_bytes += 67108864
    actual_fs_in_bytes += 102400  # add 100kb for linux to be able to boot
    bash(f"truncate --size={actual_fs_in_bytes} ./HyperOS.bin")

    # Rars are bigger but natively supported by the ChromeOS file manager
    bash("rar a HyperOS.bin.rar -m5 HyperOS.bin")


def chroot(command: str) -> None:
    if args.verbose:
        bash(f'chroot /mnt/hyperos /bin/sh -c "{command}"')
    else:
        bash(f'chroot /mnt/hyperos /bin/sh -c "{command}" 2>/dev/null 1>/dev/null')  # supress all output


if __name__ == "__main__":
    if os.geteuid() == 0 and not path_exists("/tmp/.hyperos_root_ok"):
        print_error("Please start the script as non-root/without sudo")
        exit(1)

    args = process_args()  # process args before elevating to root for better ux

    # Restart script as root
    if not os.geteuid() == 0:
        # create empty file to confirm script was started as non-root
        with open("/tmp/.hyperos_root_ok", "w") as file:
            file.write("")
        sudo_args = ['sudo', sys.executable] + sys.argv + [os.environ]
        os.execlpe('sudo', *sudo_args)

    # delete file to confirm script was started as root
    rmfile("/tmp/.hyperos_root_ok")

    # parse arguments
    dev_release = args.dev_build
    kernel_type = "stable"
    if args.dev_build:
        print_warning("Using dev release")
    if args.alt:
        print_warning("Using alt kernel")
        kernel_type = "alt"
    if args.exp:
        print_warning("Using experimental kernel")
        kernel_type = "exp"
    if args.mainline:
        print_warning("Using mainline kernel")
        kernel_type = "mainline"
    if args.verbose:
        print_warning("Verbosity increased")
        set_verbose(True)  # set verbose in functions.py


    # prepare mount
    mkdir("/mnt/hyperos", create_parents=True)

    image_props = prepare_image()
    bootstrap_rootfs(image_props[0])
    configure_rootfs()
    customize_kde()

    # Unmount image to prevent tar error: "file changed as we read it"
    bash("umount -f /mnt/hyperos")
    sleep(5)  # wait for umount to finish
    compress_image(image_props[1])

    bash(f"losetup -d {image_props[1]}")  # unmount image

    print_header("Image creation completed successfully!")
