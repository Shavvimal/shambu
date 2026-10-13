# Disclaimer

## Responsible use

shambu exists for lawful work with 3D printing software and hardware you own or are authorised to use: preparing
print projects, inspecting and editing your own project files, and reading the status of your own printer. The
project will not help with circumventing licensing, authentication or access controls on devices or services you are
not authorised to use, with copying proprietary code, or with sending jobs to printers that are not yours.

## User responsibility

You are responsible for how you use shambu, including every command and action you direct an AI agent to run with it.
The fact that software is publicly available, or that a device answers on your network, does not by itself grant you
permission to use it. Review a generated project before you print it. A slicer project can damage a printer or waste
material if its settings are wrong.

## Device and network operations

shambu reads a Bambu Lab printer over the local network using the printer's own LAN access code, which it reads from
Bambu Studio's configuration on the same machine. It subscribes to the printer's status topic and sends one status
request. It never starts, pauses, stops or modifies a print, never uploads files, and never sends any other command.
Protect your access code, your network and anything you capture; do not run the tool against a printer you do not
control.

## Reverse engineering and analysis results

Parts of shambu encode findings from reading the Bambu Studio source code (AGPL-3.0,
https://github.com/bambulab/BambuStudio), inspecting the shipped binary and running experiments against its command
line. This was done for interoperability: to drive the program's own command line correctly and to read and write its
project files. Analysis of this kind can be incomplete or wrong, and newer versions of Bambu Studio can change any of
it. Verify consequential conclusions yourself. Nothing in this repository grants any right to Bambu Studio, Bambu Lab
firmware, or their data.

## Third-party software and licences

shambu does not include Bambu Studio code. It requires a locally installed copy of Bambu Studio, which you obtain and
use under Bambu Lab's own terms. Third-party components keep their own licences. Product names such as Bambu Lab,
Bambu Studio and X2D belong to their owners and are used only to identify the software and hardware this tool works
with.

## No endorsement

shambu is an independent project. It is not affiliated with, sponsored by or endorsed by Bambu Lab, Anthropic or any
other party named in this repository. Credits to third-party projects do not imply their endorsement.

## Warranty and liability

shambu is provided "AS IS", without warranty of any kind, under the MIT License in `LICENSE`. The warranty and
liability terms of that licence apply, subject to applicable law.

## Relationship to the licence

This document does not amend the MIT License and grants no rights to any third-party software, device or data.
