
#  Moonraker - API Web Server for Klipper

Moonraker is a Python 3 based web server that exposes APIs with which
client applications may use to interact with the 3D printing firmware
[Klipper](https://github.com/KevinOConnor/klipper). Communication between
the Klippy host and Moonraker is done over a Unix Domain Socket.  Tornado
is used to provide Moonraker's server functionality.

Documentation for users and developers can be found on
[Read the Docs](https://moonraker.readthedocs.io/en/latest/).

### Clients

Note that Moonraker does not come bundled with a client, you will need to
install one.  The following clients are currently available:

- [Mainsail](https://github.com/mainsail-crew/mainsail) by [Mainsail-Crew](https://github.com/mainsail-crew)
- [Fluidd](https://github.com/fluidd-core/fluidd) by Cadriel
- [KlipperScreen](https://github.com/jordanruthe/KlipperScreen) by jordanruthe
- [mooncord](https://github.com/eliteSchwein/mooncord) by eliteSchwein

### Raspberry Pi Images

Moonraker is available pre-installed with the following Raspberry Pi images:

- [MainsailOS](https://github.com/mainsail-crew/MainsailOS) by [Mainsail-Crew](https://github.com/mainsail-crew)
  - Includes Klipper, Moonraker, and Mainsail
- [FluiddPi](https://github.com/fluidd-core/FluiddPi) by Cadriel
  - Includes Klipper, Moonraker, and Fluidd

### Docker Containers

The following projects deploy Moonraker via Docker:

- [prind](https://github.com/mkuf/prind) by mkuf
  - A suite of containers which allow you to run Klipper in
    Docker.  Includes support for OctoPrint and Moonraker.

### Changes

Please refer to the [changelog](https://moonraker.readthedocs.io/en/latest/changelog)
for a list of notable changes to Moonraker.

---

# Moonraker for Snapmaker U1

This is a project developed based on Moonraker specifically for Snapmaker U1.

### Development

Moonraker can be installed onto the target system by simply copying it.

- First, you need to log in to the system and stop the moonraker process

  ```
  /etc/init.d/S61moonraker stop
  ```

- Second, copy the python source files and scripts in the "moonraker" and "scripts" subdirectories to the corresponding subdirectories in the device directory "/home/lava/moonraker".

- Third, restart the device or restart the moonraker service. The device uses an overlay file system, so a temporary file /oem/.debug needs to be created to ensure that the updated moonraker is not overwritten. You can restart the moonraker service by using the following command

  ```
  /etc/init.d/S61moonraker start
  ```
