# Still to do:
- For systemd unit files, decide how to most smartly run python scripts

# Ceating ***udev*** rules for picturec devices
- The ArduinoUNO (currentduino) and ArduinoMEGA (hemttempAgent) udev rules are based on the
  serial numbers from the devices themselves.
- The SIM921 and SIM960 (AC Resistance Bridge / PID Controller) use RS232-to-USB converters, 
  so their udev rules are based on the FTDI chips in the USB-to-RS232 cable.
    - Each cable will be labelled with the device it goes to to avoid confusion.

# How to properly configure the cp210x driver for unsupported USB devices!!
- As of 26 July 2022, the LakeShore 240 and Lake shore 372 are not supported natively by Linux
- Not to worry! We can slightly modify the driver module and make everything alright (see this post for a good intro
    of what we will be doing https://www.silabs.com/community/interface/forum.topic.html/linux_cannot_identif-PB7r)
- First, from this link (https://github.com/torvalds/linux/blob/master/drivers/usb/serial/cp210x.c) find the version of
    the cp210x.c file that matches the linux kernel you have (e.g. if using Linux 5.15.10, use the cp210x.c from tag v5.15)
- Copy the contents of this file into docs/manuals/drivers/linuxlakeshoredriver/cp210x.c (you  may move them anywhere, 
    but this file currently exists and there is an appropriate Makefile in the same director)
- In 'cp210x.c' add the VID/PID of the LS240 (1FB9, 0205) and LS372 (1FB9, 0305) to the list like below
    ```
    { USB_DEVICE(0x1FB9, 0x0201) }, /* Lake Shore Model 219 Temperature Monitor */
    { USB_DEVICE(0x1FB9, 0x0202) }, /* Lake Shore Model 233 Temperature Transmitter */
    { USB_DEVICE(0x1FB9, 0x0203) }, /* Lake Shore Model 235 Temperature Transmitter */
    { USB_DEVICE(0x1FB9, 0x0205) }, /* Lake Shore Model 240 Temperature Monitor <---Edit This line */ 
    { USB_DEVICE(0x1FB9, 0x0300) }, /* Lake Shore Model 335 Temperature Controller */
    { USB_DEVICE(0x1FB9, 0x0305) }, /* Lake Shore Model 372 AC Bridge <--- Add this line as well */
    ```
- After adding in the appropriate USB_DEVICE VID and PID, recompile the kernel object file.
  - NOTE: Make sure that you are using compatible gcc versions! 
    - The linux kernel is compliled with a specific version that you can look up by running the command `cat /proc/version`.
    - To find the gcc used when you run `make`, run `gcc -v`
    - If the output from `gcc -v` is the same version (or higher) than that from `cat /proc/version`, you're okay and can run
       everything normally. If not, you need to ensure that you use the right gcc.
    - To set a higher gcc, first run `update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-<version-needed> 50` 
       where `<version-needed` is the release of gcc that that linux kernel used (e.g. if it used `11.2.0`, `<version-needed>`=`11`).
       The final number just sets a higher priority than the default (which is 60)
    - Once installed, run `sudo update-alternatives --config gcc` and follow the prompts to use whichever gcc compiler you
       need. The default will be what you saw from `gcc -v`, and you should have at least 1 more option from what you just
       installed.
    - Once you are confident you are compiling with the proper gcc version, proceed to the next steps
  - In the directory with the cp210x.c file, run `make clean`. You should now only have the cp210x.c and Makefile 
  - In the directory with the cp210x.c file (and the other requisite files) run `make all`
  - If there are no errors, you should now have all the files you will need
- Next, copy the cp210x.ko file to where the kernel modules exist on your machine.
  - Run `sudo cp cp210x.ko /lib/modules/$(uname -r)/kernel/drivers/usb/serial`
- Run `sudo insmod /lib/modules/$(uname -r)/kernel/drivers/usb/serial/cp210x.ko` (This should fail, as the file exists)
- Run `sudo modprobe cp210x` to restart the module. 
  - However, no changes will take effect until re-plugging the USB devices (i.e. your LS240 won't show up until replugging it in)
  - This will only get you the cp210x module started during this boot. If you reboot and do nothing else, then cp210x will not restart on a reboot
  - To configure automatic start at boot, see following note
- Now, if you are NOT using secure boot, edit '/etc/modules' to contain 1 line that says 'cp210x'.
- If you ARE using secure boot, still edit '/etc/modules' to contain the line 'cp210x', but we also need to manually
    sign the files (essentially signing off that they're trusted)
    - See the following link for instructions : https://ubuntu.com/blog/how-to-sign-things-for-secure-boot
    - Create a file called openssl.cnf
        - In the file paste the following:
          ```
            # This definition stops the following lines choking if HOME isn't defined.
            HOME                    = .
            RANDFILE                = $ENV::HOME/.rnd
            [ req ]
            distinguished_name      = req_distinguished_name
            x509_extensions         = v3
            string_mask             = utf8only
            
            [ req_distinguished_name ]
            commonName              = Secure Boot Signing
            
            [ v3 ]
            subjectKeyIdentifier    = hash
            authorityKeyIdentifier  = keyid:always,issuer
            basicConstraints        = critical,CA:FALSE
            extendedKeyUsage        = codeSigning,1.3.6.1.4.1.311.10.3.6,1.3.6.1.4.1.2312.16.1.2
            nsComment               = "OpenSSL Generated Certificate"
          ```
    - Once created run from the command line
        - 'openssl req -config ./openssl.cnf -new -x509 -newkey rsa:2048 -nodes -days 36500 -outform DER -keyout "MOK.priv" -out "MOK.der"'
        - Note that -keyout and -out specify the paths to the private and public keys that you will need (.priv and .der)
    - Once completed, run 'sudo mokutil --import MOK.der'
        - This will prompt a password for when you enroll the key.
    - After making a password (keep it simple, you only need it once, it's temporary) reboot the computer.
    - While rebooting, it will take you to a blue screen that says MokManager, follow the prompts to 'Enroll MOK'
    - After following the prompts, it will reboot again and start the computer up
        - With the computer started, run 'sudo cat /proc/keys' and make sure there is one that has the same commonName as you entered.
    - Now you can sign files!
        - To sign the specific module we need, run the following
            'sudo kmodsign sha512 /path/to/MOK.priv /path/to/MOK.der /lib/modules/$(uname -r)/kernel/drivers/usb/serial/cp210x.c'
    - At this point, you should be able to connect to the LakeShore240 via serial, even with secure boot on and
        without manually starting the module.
    - NOTE : If the module is not started up immediately after signing it, you can either reboot (make sure it's in
        the '/etc/modules' file) or run 'sudo modprobe cp210x', which will load it without rebooting

# Compiling the fliusb.ko file to talk to it
- Make a directory to store the files you will need
  - mkdir rts2fliusb
- First, clone the git repository at https://github.com/RTS2/fliusb.git
  - Run 'git clone https://github.com/RTS2/fliusb.git'
- Enter the subdirectory with the fliusb c code
  - Run cd fliusb/fliusb/
- Edit the 'fliusb.c' file
  - The first block of include statements should be modified
    ```
    #include <linux/version.h>
    #include <linux/init.h>
    #include <linux/module.h>
    #include <linux/mutex.h>
    #include <linux/kernel.h>
    #include <linux/kref.h>
    #include <linux/errno.h>
    #include <linux/usb.h>
    #include <linux/fs.h>
    #include <linux/fcntl.h>
    #include <asm/uaccess.h>
    #include <linux/slab.h>
    #include <linux/mmap_lock.h>  /* <- ADD THIS LINE FOR Linux-5.x.x Compatibility */
    ```
  - Change the lines
    '''
    down_read(&current->mm->mmap_sem); (line 336)
    ...
    up_read(&current->mm->mmap_sem); (line 349)
    '''
    to
    '''
    down_read(&current->mm->mmap_lock); (line 336)
    ...
    up_read(&current->mm->mmap_lock); (line 349)
    '''
    to account for an update between Linux-3.x.x and Linux-4.x.x and above
  - Save the file
  - Run 'make clean'
  - Run 'sudo make'
    - You should now have many more files in the directory including 'fliusb.ko'
  - Run 'mkdir /lib/modules/5.15.0-41-generic/extra/'
  - Copy the fliusb.ko file into this new directory
    - Run 'sudo cp fliusb.ko /lib/modules/5.15.0-41-generic/extra/'
  - Insert the module so it is discoverable
    - Run 'sudo insmod /usr/lib/modules/5.15.0-41-generic/extra/fliusb.ko'
  - Run 'sudo depmod -a'
  - Run 'sudo modprobe fliusb.ko'
    - This will start the fliusb module and enable communication with the FLI filter wheel

# Recompiling the ftdi_sio.ko file to talk with conex controller
- This process is inspired by the cp210x modification required to talk with natively unsupported lakeshore devices.
- From 'https://github.com/torvalds/linux/tree/master/drivers/usb/serial' download 'ftdi_sio.c', 'ftdi_sio.h', 'ftdi_sio_ids.h'
  - Make sure that one doesn't only use the '/master/' branch. Choose the proper version for your linux kernel
  - e.g. if you use kernel '5.15.0-41-generic', you would use tag 'v5.15'
- Move these files to '~/mkidcore/docs/manuals/drivers/conexdriver'
- Modify 'ftdi_sio_ids.h'
  - In the Newport Cooperation block, add '#define NEWPORT_CONEX_AGAP_PID		0x3008'
  '''
    /*
    * Newport Cooperation (www.newport.com)
    */
    #define NEWPORT_VID			0x104D
    #define NEWPORT_AGILIS_PID		0x3000
    #define NEWPORT_CONEX_CC_PID		0x3002
    #define NEWPORT_CONEX_AGP_PID		0x3006
    #define NEWPORT_CONEX_AGAP_PID		0x3008
  '''
- 'ftdi_sio.h' does not require modification
- Modify 'ftdi_sio.c'
  - In the Newport block, add '{ USB_DEVICE(NEWPORT_VID, NEWPORT_CONEX_AGAP_PID) },'
  '''
  { USB_DEVICE(NEWPORT_VID, NEWPORT_AGILIS_PID) },
  { USB_DEVICE(NEWPORT_VID, NEWPORT_CONEX_CC_PID) },
  { USB_DEVICE(NEWPORT_VID, NEWPORT_CONEX_AGP_PID) },
  { USB_DEVICE(NEWPORT_VID, NEWPORT_CONEX_AGAP_PID) },
  { USB_DEVICE(INTERBIOMETRICS_VID, INTERBIOMETRICS_IOBOARD_PID) },
  { USB_DEVICE(INTERBIOMETRICS_VID, INTERBIOMETRICS_MINI_IOBOARD_PID) },
  '''
- Using the 'Makefile' in '~/mkidcore/docs/manuals/drivers/conexdriver' run 'make'
- Copy the original ftdi_sio.ko file to the '~/original_ko_files' directory (following the method described for the cp210x driver above)
- Follow the same commands to copy this to the same location as the cp210x driver above
- Sign the module if desired
- Add the module to '/etc/modules' if desired