#!/usr/bin/env python3
#
# Copyright (c) 2017 Philip Langdale
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import argparse
import asyncio
import functools
import os
from pathlib import Path
import signal
import time


import daemon
import evdev
from evdev import ecodes, InputDevice, UInput
from xdg import BaseDirectory
import yaml

import inspect
from pprint import pprint

DEFAULT_RATE = .1  # seconds
DUAL_ROLE_PRESS_WAIT = .1
DUAL_ROLE_PRESS_WAIT_LONG = .3
repeat_tasks = {}
remapped_tasks = {}

MAX_SPEED = 1000  # px/sec
MIN_SPEED = 200
POINTER_RATE = 10  # ms
POINTER_ACCELERAION_DELAY = 50  # ms
POINTER_ACCELERATION_TIME = 300  # ms
POINTER_RETENTION = POINTER_RATE * 2
_pointer_speed = None
pointer_lastaccess = time.time()
pointer_start = None
idle_time = time.time()


def pointer_speed():
    global pointer_lastaccess
    global _pointer_speed
    global pointer_start

    now = time.time()

    if now - pointer_lastaccess > POINTER_RETENTION / 1000:
        _pointer_speed = MIN_SPEED
        pointer_start = now

    elapsed = now - pointer_start

    """

    _pointer_speed = (MIN_SPEED / 1000 + elapsed_ms * (MAX_SPEED - MIN_SPEED) / 1000 / POINTER_ACCELERATION_TIME) * POINTER_RATE
    max_pointer_speed = MAX_SPEED / 1000 * POINTER_RATE
    if _pointer_speed >= MAX_SPEED / 1000 * POINTER_RATE:
        _pointer_speed = max_pointer_speed
    """
    if now - pointer_start > POINTER_ACCELERAION_DELAY / 1000:
        _pointer_speed = MIN_SPEED + elapsed * (MAX_SPEED - MIN_SPEED) / (POINTER_ACCELERATION_TIME - POINTER_ACCELERAION_DELAY) * 1000
    if _pointer_speed > MAX_SPEED:
        _pointer_speed = MAX_SPEED
    pointer_lastaccess = now
    return int(_pointer_speed / 1000 * POINTER_RATE)


def pointer_speed_rev():
    return -pointer_speed()


def press_key(output, event):
    for val in (1, 0):
        event.value = val
        output.write_event(event)
        output.syn()


@asyncio.coroutine
def inactivity_tremor(output):
    while True:
        if time.time() - idle_time > 5:
            for val in (1, -1):
                output.write(evdev.ecodes.EV_REL, evdev.ecodes.REL_X, val)
                output.syn()
        yield from asyncio.sleep(1)


@asyncio.coroutine
def handle_events(input, output, remappings, modifier_groups):
    global idle_time
    active_group = {}
    pressed_keys = {}
    while True:
        events = yield from input.async_read()  # noqa
        idle_time = time.time()

        for event in events:
            if 'name' not in active_group:
                active_mappings = remappings
            else:
                active_mappings = remappings.copy()
                active_mappings.update(modifier_groups[active_group['name']])
            if (event.code not in repeat_tasks and (event.code == active_group.get('code') or
                    (event.code in active_mappings and 'modifier_group' in active_mappings.get(event.code)[0]))):
                if event.value == 1:
                    active_group['name'] = active_mappings[event.code][0]['modifier_group']
                    active_group['code'] = event.code
                    active_group['entered'] = time.time()
                elif event.value == 0:
                    if 'entered' not in active_group:
                        output.write_event(event)
                        output.syn()
                    elif time.time() - active_group['entered'] < DUAL_ROLE_PRESS_WAIT_LONG:
                        press_key(output, event)
                        #repeat_tasks[event.code] = asyncio.ensure_future(
                        #    repeat_event(event, .4, 1, [1, 0], output, event.code))
                    active_group = {}
            else:
                if event.code in active_mappings and 'modifier_group' not in active_mappings.get(event.code)[0]:
                    active_group['is_used'] = True
                key_mapping = active_mappings.get(event.code, [])
                if event.value == 1 and event.code in active_mappings:
                    pressed_keys[event.code] = active_mappings[event.code]
                    remap_event(output, event, key_mapping)
                elif event.value == 0 and event.code in pressed_keys:
                    key_mapping = pressed_keys.pop(event.code)
                    remap_event(output, event, key_mapping)
                else:
                    output.write_event(event)
                    output.syn()


@asyncio.coroutine
def repeat_event(event, rate, count, values, output, original_code=None):
    if count == 0:
        count = -1
    while count != 0:
        count -= 1
        for value in values:
            if callable(value):
                value = value()
            event.value = value
            output.write_event(event)
            output.syn()
        yield from asyncio.sleep(rate)
    #del repeat_tasks[original_code]


DUAL_ROLE_KEYS_PRESSED = dict()


def remap_event(output, event, event_remapping):
    for remapping in event_remapping:
        if {'tap', 'hold'} <= remapping.keys():
            event.code = remapping['hold']
            output.write_event(event)
            output.syn()
            if event.value == 1:
                DUAL_ROLE_KEYS_PRESSED[event.code] = time.time()
            elif event.value == 0:
                press_time = DUAL_ROLE_KEYS_PRESSED.pop(event.code, None)
                if press_time and time.time() - press_time < DUAL_ROLE_PRESS_WAIT:
                    original_code = event.code
                    event.code = remapping['tap']
                    press_key(output, event)
                    event.code = original_code
            continue
        elif 'shell' in remapping:
            os.system(remapping['shell'])
            continue
        elif 'code' not in remapping:
            output.write_event(event)
            output.syn()
            continue
        original_code = event.code
        event.code = remapping['code']
        event.type = remapping.get('type', None) or event.type
        values = remapping.get('value', None) or [event.value]
        repeat = remapping.get('repeat', False)
        delay = remapping.get('delay', False)
        if not repeat and not delay:
            for value in values:
                event.value = value
                output.write_event(event)
                output.syn()
        else:
            key_down = event.value == 1
            key_up = event.value == 0
            count = remapping.get('count', 0)

            if not (key_up or key_down):
                return
            if delay:
                if original_code not in remapped_tasks or remapped_tasks[original_code] == 0:
                    if key_down:
                        remapped_tasks[original_code] = count
                else:
                    if key_down:
                        remapped_tasks[original_code] -= 1

                if remapped_tasks[original_code] == count:
                    output.write_event(event)
                    output.syn()
            elif repeat:
                # count > 0  - ignore key-up events
                # count is 0 - repeat until key-up occurs
                ignore_key_up = count > 0

                if ignore_key_up and key_up:
                    return
                rate = remapping.get('rate', DEFAULT_RATE)
                repeat_task = repeat_tasks.pop(original_code, None)
                if repeat_task:
                    repeat_task.cancel()
                if key_down:
                    repeat_tasks[original_code] = asyncio.ensure_future(
                        repeat_event(event, rate, count, values, output, original_code))


# Parses yaml config file and outputs normalized configuration.
# Sample output:
#  'devices': [{
#    'input_fn': '',
#    'input_name': '',
#    'input_phys': '',
#    'output_name': '',
#    'remappings': {
#      42: [{             # Matched key/button code
#        'code': 30,      # Mapped key/button code
#        'type': EV_REL,  # Overrides received event type [optional]
#                         # Defaults to EV_KEY
#        'value': [1, 0], # Overrides received event value [optional].
#                         # If multiple values are specified they will
#                         # be applied in sequence.
#                         # Defaults to the value of received event.
#        'repeat': True,  # Repeat key/button code [optional, default:False]
#        'delay': True,   # Delay key/button output [optional, default:False]
#        'rate': 0.2,     # Repeat rate in seconds [optional, default:0.1]
#        'count': 3       # Repeat/Delay counter [optional, default:0]
#                         # For repeat:
#                         # If count is 0 it will repeat until key/button is depressed
#                         # If count > 0 it will repeat specified number of times
#                         # For delay:
#                         # Will suppress key/button output x times before execution [x = count]
#                         # Ex: count = 1 will execute key press every other time
#      }]
#    },
#    'modifier_groups': {
#        'mod1': { -- is the same as 'remappings' --}
#    }
#  }]
def load_config(config_override):
    conf_path = None
    if config_override is None:
        for dir in BaseDirectory.load_config_paths('evdevremapkeys'):
            conf_path = Path(dir) / 'config.yaml'
            if conf_path.is_file():
                break
        if conf_path is None:
            raise NameError('No config.yaml found')
    else:
        conf_path = Path(config_override)
        if not conf_path.is_file():
            raise NameError('Cannot open %s' % config_override)

    with open(conf_path.as_posix(), 'r') as fd:
        config = yaml.safe_load(fd)
        for device in config['devices']:
            device['remappings'] = normalize_config(device['remappings'])
            device['remappings'] = resolve_ecodes(device['remappings'])
            if 'modifier_groups' in device:
                for group in device['modifier_groups']:
                    device['modifier_groups'][group] = normalize_config(device['modifier_groups'][group])
                    device['modifier_groups'][group] = resolve_ecodes(device['modifier_groups'][group])
    return config


# Converts general config schema
# {'remappings': {
#     'BTN_EXTRA': [
#         'KEY_Z',
#         'KEY_A',
#         {'code': 'KEY_X', 'value': 1}
#         {'code': 'KEY_Y', 'value': [1,0]]}
#     ]
# }}
# into fixed format
# {'remappings': {
#     'BTN_EXTRA': [
#         {'code': 'KEY_Z'},
#         {'code': 'KEY_A'},
#         {'code': 'KEY_X', 'value': [1]}
#         {'code': 'KEY_Y', 'value': [1,0]]}
#     ]
# }}
def normalize_config(remappings):
    norm = {}
    for key, mappings in remappings.items():
        new_mappings = []
        for mapping in mappings:
            if type(mapping) is str:
                new_mappings.append({'code': mapping})
            else:
                normalize_value(mapping)
                new_mappings.append(mapping)
        norm[key] = new_mappings
    return norm


def normalize_value(mapping):
    value = mapping.get('value')
    if value is None or type(value) is list:
        return
    elif type(value) is str and 'POINTER_SPEED' in value:
        if value.startswith('-'):
            mapping['value'] = pointer_speed_rev
        else:
            mapping['value'] = pointer_speed

    mapping['value'] = [mapping['value']]


def resolve_ecodes(by_name):
    def resolve_mapping(mapping):
        for key in ('code', 'type', 'tap', 'hold'):
            if key in mapping:
                mapping[key] = ecodes.ecodes[mapping[key]]
        return mapping
    return {ecodes.ecodes[key]: list(map(resolve_mapping, mappings))
            for key, mappings in by_name.items()}


def find_input(device):
    name = device.get('input_name', None)
    phys = device.get('input_phys', None)
    fn = device.get('input_fn', None)

    if name is None and phys is None and fn is None:
        raise NameError('Devices must be identified by at least one ' +
                        'of "input_name", "input_phys", or "input_fn"')

    devices = [InputDevice(fn) for fn in evdev.list_devices()]
    for input in devices:
        if name is not None and input.name != name:
            continue
        if phys is not None and input.phys != phys:
            continue
        if fn is not None and input.fn != fn:
            continue
        return input
    return None


def register_device(device):
    input = find_input(device)
    if input is None:
        raise NameError("Can't find input device")
    input.grab()

    caps = input.capabilities()
    # EV_SYN is automatically added to uinput devices
    del caps[ecodes.EV_SYN]

    remappings = device['remappings']
    extended = set(caps[ecodes.EV_KEY])

    modifier_groups = []
    if 'modifier_groups' in device:
        modifier_groups = device['modifier_groups']

    def flatmap(lst):
        return [l2 for l1 in lst for l2 in l1]

    for remapping in flatmap(remappings.values()):
        if 'code' in remapping:
            extended.update([remapping['code']])

    for group in modifier_groups:
        for remapping in flatmap(modifier_groups[group].values()):
            if 'code' in remapping:
                extended.update([remapping['code']])

    caps[ecodes.EV_KEY] = list(extended)
    caps[ecodes.EV_REL] = [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL, ecodes.REL_HWHEEL]
    output = UInput(caps, name=device['output_name'])
    loop = asyncio.get_event_loop()
    asyncio.ensure_future(handle_events(input, output, remappings, modifier_groups))
    loop.run_until_complete(inactivity_tremor(output))


@asyncio.coroutine
def shutdown(loop):
    tasks = [task for task in asyncio.Task.all_tasks() if task is not
             asyncio.tasks.Task.current_task()]
    list(map(lambda task: task.cancel(), tasks))
    yield from asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


def run_loop(args):
    config = load_config(args.config_file)
    for device in config['devices']:
        register_device(device)

    if 'run_shell_on_start' in config:
        os.system(config['run_shell_on_start'])

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM,
                            functools.partial(asyncio.ensure_future,
                                              shutdown(loop)))

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.run_until_complete(asyncio.ensure_future(shutdown(loop)))
    finally:
        loop.close()


def list_devices():
    devices = [InputDevice(fn) for fn in evdev.list_devices()]
    for device in reversed(devices):
        yield [device.fn, device.phys, device.name]


def read_events(req_device):
    for device in list_devices():
        # Look in all 3 identifiers + event number
        if req_device in device or req_device == device[0].replace("/dev/input/event", ""):
            found = evdev.InputDevice(device[0])

    if 'found' not in locals():
        print("Device not found. \nPlease use --list-devices to view a list of available devices.")
        return

    print(found)
    print("To stop, press Ctrl-C")

    for event in found.read_loop():
        try:
            if event.type == evdev.ecodes.EV_KEY:
                categorized = evdev.categorize(event)
                if categorized.keystate == 1:
                    keycode = categorized.keycode if type(categorized.keycode) is str else \
                            " | ".join(categorized.keycode)
                    print("Key pressed: %s (%s)" % (keycode, categorized.scancode))
        except KeyError:
            if event.value:
                print("Unknown key (%s) has been pressed." % event.code)
            else:
                print("Unknown key (%s) has been released." % event.code)


def main():
    parser = argparse.ArgumentParser(description='Re-bind keys for input devices')
    parser.add_argument('-d', '--daemon',
                        help='Run as a daemon', action='store_true')
    parser.add_argument('-f', '--config-file',
                        help='Config file that overrides default location')
    parser.add_argument('-l', '--list-devices', action='store_true',
                        help='List input devices by name and physical address')
    parser.add_argument('-e', '--read-events', metavar='EVENT_ID',
                        help='Read events from an input device by either name, physical address or number.')

    args = parser.parse_args()
    if args.list_devices:
        print("\n".join(['%s:\t"%s" | "%s' % (fn, phys, name) for (fn, phys, name) in list_devices()]))
    elif args.read_events:
        read_events(args.read_events)
    elif args.daemon:
        with daemon.DaemonContext():
            run_loop(args)
    else:
        run_loop(args)


if __name__ == '__main__':
    main()

