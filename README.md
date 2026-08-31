# Harmony Hub Replacement

Logitech Harmony Hub got discontinued, but your Harmony Companion remote still works fine — you just have nothing left to talk to it.
This project listens to your existing Harmony remote and lets you decide what every button does, all from a simple app on your phone or computer.

## What it can do

- **Pair & Listen to Harmony Remote** — Pair your Remote with or without a Hub in place
- **Scenes** — Watch TV, Listen to Music, Start Gaming - Starting one Scene changes what every button on the remote directs to
- **Scenes can check before they act** — "only turn the TV on if it's off", "wait for the receiver to actually come on before picking the input" - no more guessing with a fixed delay
- **A picture of your actual remote** — set things up by tapping buttons on
  a picture of the remote, not by memorizing button names - Everything can be controlled from the UI aswell
- **Works from your phone or a browser** 
- **Talks to the equipment you already own** — Home Assistant MQTT Publisher, Android TV /
  Google TV, Denon/Marantz AV receivers, LG webOS TVs, and any old infrared
  gear, with webhooks and local commands for anything else

## App Screenshots
<div style="display: flex; gap: 8px; flex-wrap: wrap;">
  <img src="docs/1.jpg" alt="App screenshot 1" style="width: 22%;">
  <img src="docs/2.jpg" alt="App screenshot 2" style="width: 22%;">
  <img src="docs/3.jpg" alt="App screenshot 3" style="width: 22%;">
  <img src="docs/4.jpg" alt="App screenshot 4" style="width: 22%;">
</div>

## Running it

The first time, set everything up:

```bash
python -m venv venv
venv\Scripts\activate
pip install -e .
cd app
flutter build web
cd ..
```

After that, starting it up is just one command:

```bash
harmony-hub
```

Then open **http://localhost:8765** in your browser — that's the app.
## Hardware Requirements

1. NRF24L01 module
2. > Raspberry Pi 3

or (should work but untested)

1. ESP32 with NRF24L01 (Transmit data to Backend)
2. any Linux device which runs the WebUI and Backend

## Getting started

1. **Turn on the receiver.** Boot up the Service
2. **Point the app at your remote.** On the **Settings** tab, hit
   **Find my remote**. Pair it without the Hub at all - or with a Harmony Hub already in place
3. **Teach it your buttons.** Still on Settings, hit **Learn the remote**.
   Press each button on your Harmony remote once; it appears on screen,
   usually with the right name already filled in. Correct anything that
   looks wrong, then Save. You only do this once.
4. **Add your equipment.** On the **Devices** tab, you can freely add anything listed there. 
Currently Supported:
LG WebOS, Denon/Marantz, Android TV/Google TV, HomeAssistant Devices/Scenes/Entities, Learn IR Signals from older Remotes
5. **Create scenes.** On the **Scenes** tab, make one for each thing you do
   — e.g. "Watch TV", "Listen Music"
6. **Map your remote.** For each scene, pick a device and tap through the
   picture of your remote to decide what each button should do. There are
   suggestions offered automatically — you can just accept them, or change
   anything you like.
7. **Start a scene**, pick up your Harmony remote, and try it. (Or use the WebUI)

## Good to know

- **If a button fires too many times, adjust the repeat timing.** There's one setting for this that covers every button at
  once — on the **Scenes** tab, tap **Default repeat timing**. Raise **Wait
  before repeating** if a quick press does something several times, or
  lower it if holding the button feels sluggish to start. A second slider
  slows the repeats down once they start.
- **Holding a button down longer can make it go faster.** The same
  **Default repeat timing** dialog has a **Speed up the longer it is
  held** slider — off by default. Turn it up and a button like Volume
  starts at the normal repeat rate and ramps up to that many times faster
  the longer you keep holding it
- **Both of these "Repeat" Modifications, can be configured individually for each button**
- **The +/- keys always mean "the thing I just touched."** Toggle a light or
  a switch with one of the SmartHome keys, and +/- steps that same thing up
  or down — just like the original Remote. The Live tab  shows what they're currently pointed at.
- **A scene's start/stop steps can branch on a condition.** Add an "If /
  Otherwise" step and it can check a device's current state. Remember states and more.
  Scene Transistions (from/to) is also in place
- **Works on a phone screen too.** The layout adjusts automatically, so
  you're not stuck needing a full computer to make a change. (Remote only Fullscreen available and is remembered throught Sessions)

If in doubt, the **Settings** tab is always the place to look first.

Thanks to the following repo, this Project was able to be build:
https://github.com/joakimjalden/Harmoino