# Still to do:
- Order/configure switches to use them with the PICTURE-C electronics rack.
    - Determine how we want to control the power.
    - Figure out how we want to account for instruments being off in the software side of things.
- For systemd unit files, decide how to most smartly run python scripts

# Ceating ***udev*** rules for picturec devices
- The ArduinoUNO (currentduino) and ArduinoMEGA (hemttempAgent) udev rules are based on the
  serial numbers from the devices themselves.
- The SIM921 and SIM960 (AC Resistance Bridge / PID Controller) use RS232-to-USB converters, 
  so their udev rules are based on the FTDI chips in the USB-to-RS232 cable.
    - Each cable will be labelled with the device it goes to to avoid confusion.

# How to properly configure the driver for the LakeShore240 USB!!
- As of 14 October 2020, the LakeShore 240 Temperature Monitor is the only Lake Shore product that is NOT 
  supported natively by Linux
- Not to worry! We can slightly modify the driver module and make everything alright (see this post for a good intro
    of what we will be doing https://www.silabs.com/community/interface/forum.topic.html/linux_cannot_identif-PB7r)
- First, from this link (https://www.silabs.com/products/development-tools/software/usb-to-uart-bridge-vcp-drivers)
    you can download driver source code so you can manually add it in.
- Next, from the zipped directory you download, unzip into a directory (currently this is
    'mkidcontrol/docs/hardware_reference_documentation/drivers/lakeshoredriver/linuxlakeshoredriver') where you will find a file called cp210x.c
- In 'cp210x.c' add the VID/PID of the LS240 (1FB9, 0205) to the list like below
    ```
    { USB_DEVICE(0x1FB9, 0x0201) }, /* Lake Shore Model 219 Temperature Monitor */
    { USB_DEVICE(0x1FB9, 0x0202) }, /* Lake Shore Model 233 Temperature Transmitter */
    { USB_DEVICE(0x1FB9, 0x0203) }, /* Lake Shore Model 235 Temperature Transmitter */
    { USB_DEVICE(0x1FB9, 0x0205) }, /* Lake Shore Model 240 Temperature Monitor <---Edit This line */ 
    { USB_DEVICE(0x1FB9, 0x0300) }, /* Lake Shore Model 335 Temperature Controller */
    ```
- After adding in the appropriate USB_DEVICE VID and PID, you need to recompile the kernel object file.
  - If there is already a cp210x.ko file, move it somewhere it will not be overwritten or modified 
  - In the directory with the cp210x.c file (and the other requisite files) run `make all cp210x`
  - This should create the new cp210x.ko file you will need
- Once this is done, you need to copy the cp210x.ko file to where the kernel modules exist on your machine.
    Note, this may require sudo/write permissions to the directory
    - For Ubuntu 20.04, Linux kernel 5.3.0-59-generic, this is in '/lib/modules/5.3.0-59-generic/kernel/drivers/usb/serial'
           (to find your active kernel, just type 'uname -r' into the command line)
    - Alternatively, you can copy the file to '/lib/modules/$(uname -r)/kernel/drivers/usb/serial
- After this, you should be able to run `sudo modprobe cp210x` and it will restart the module, however, no changes will take effect until re-plugging the USB devices
  - This will only get you the cp210x module started during this boot. If you reboot and do nothing else, then cp210x will not restart on a reboot
  - To configure automatic start at boot, see the following note
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