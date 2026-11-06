#!/usr/bin/env python3
"""Private offscreen host/widget lifecycle reproduction; no live shell or setter.

Run only this script: python3 scripts/test_widget_lifecycle.py --report /tmp/lifecycle.json
Uses the existing release binary, exact repository QML, installed read-only Ui,
Commons and PluginShellApi. Only KeyboardPanel's staged surface is substituted.
The registry is a controlled no-clone identity fixture, not the installed shell.
Host lookup functions and resolveEnabledId are extracted verbatim at runtime.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

# Reuse only bounded cleanup, PNG bytes and the transparent private transport.
# Do not run the wrapper-only entrypoint probe or create bytecode in the repo.
sys.dont_write_bytecode = True
from test_service_entrypoint import PNG, Proxy, stop
from preview_bundle import stage_preview

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = Path('/usr/share/omarchy/shell')
PRODUCTION = 'io.github.henniedebruin.swiftwall'
LOCAL = 'local.swiftwall.lifecycle'
WAIT = 10


def extract_function(source, name):
    """Extract these installed brace-balanced functions, retaining exact text."""
    start = source.index('  function ' + name + '(')
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        if source[end] == '{':
            depth += 1
        elif source[end] == '}':
            depth -= 1
        end += 1
    return source[start:end]


HOST = r'''import QtQuick
import QtQuick.Window
import Quickshell
import Quickshell.Io
import qs.Commons
import "plugin" as Plugin
import "services" as Services
ShellRoot {
    id: host
    // Keep installed function bodies, including their shell.* references, exact.
    property var shell: host
    property var _services: ({})
    property var pluginRegistry: registry
    property var retainedServices: []
    property var apis: []
    property var widget: null
    property var api: null
    property var barObject: null
    property int lastCommand: 0
    @FUNCTIONS@
    QtObject {
        id: registry
        property var installedPlugins: ({})
        function isEnabled(id) { return true }
        @RESOLVE@
    }
    Component { id: apiComponent; Services.PluginShellApi {} }
    Component {
        id: barComponent
        QtObject {
            property QtObject shell: null
            property bool vertical: false
            property int barSize: 32
            property string position: "top"
            property color barForeground: "white"
            property color barBackground: "black"
            property color urgent: "red"
            property string fontFamily: "sans-serif"
            property bool foregroundAnimationEnabled: false
            function hideTooltip(owner) {}
            function showTooltip(owner, text) {}
            function switchPanelFrom(owner, direction) { return false }
        }
    }
    Window { id: scene; width: 400; height: 40; visible: true }
    FileView { id: control; path: "@CONTROL@"; blockLoading: true }
    function trace(kind, data) {
        console.log("LIFECYCLE " + JSON.stringify({kind:kind, data:data}))
    }
    function find(object, predicate, seen) {
        if (!object || seen.indexOf(object) >= 0) return null
        seen.push(object)
        if (predicate(object)) return object
        var lists = [object.children, object.data, object.contentItem]
        for (var n = 0; n < lists.length; ++n) {
            var list = lists[n]
            if (!list) continue
            if (list.length === undefined) list = [list]
            for (var i = 0; i < list.length; ++i) {
                var found = find(list[i], predicate, seen)
                if (found) return found
            }
        }
        return null
    }
    function panel() {
        return find(widget, function(o) { return o.hostWidget === widget && o.draft !== undefined }, [])
    }
    function button(text) {
        return find(panel(), function(o) { return o.text === text && typeof o.clicked === "function" }, [])
    }
    function serviceName(s) { return s ? s.objectName : null }
    function sample() {
        var w = widget, p = panel(), s = w ? w.service : null
        var pause = button("Pause") || button("Resume"), next = button("Next")
        var lookup = w && barObject && barObject.shell ? barObject.shell.serviceFor(w.moduleName) : null
        var services = []
        for (var i = 0; i < retainedServices.length; ++i) {
            var r = retainedServices[i]
            services.push({name:r.objectName, online:r.online, compatible:r.compatible,
                socket:r.socketPath, instance:r.instance, seq:r.seq, busy:r.busy,
                state:r.connectionState, error:r.error, status:r.status,
                deadline:r.countdownDeadline, serial:r.serial})
        }
        return {moduleName:w ? w.moduleName : null, barInjected:!!(w && w.bar),
            apiId:barObject && barObject.shell ? barObject.shell.pluginId : null,
            lookup:serviceName(lookup), widgetService:serviceName(s),
            panelFound:!!p, panelService:p ? serviceName(p.service) : null,
            stateText:w ? w.stateText : null, panelState:p ? p.stateText : null,
            editable:p ? p.editable : false, commandEnabled:p ? p.commandEnabled : false,
            pauseEnabled:!!(pause && pause.enabled), nextEnabled:!!(next && next.enabled),
            pauseText:pause ? pause.text : null, ticking:w ? w.ticking : false,
            countdown:w ? w.countdownText : null, seconds:w ? w.secondsRemaining : 0,
            draftRevision:p ? p.draft.baseRevision : null, services:services}
    }
    function execute(c) {
        if (c.op === "reset") {
            if (widget) widget.destroy()
            widget = null
            if (barObject) barObject.destroy()
            barObject = barComponent.createObject(host)
            api = null
            _services = ({})
        } else if (c.op === "widget") {
            var comp = Qt.createComponent(Qt.resolvedUrl(c.url || "plugin/BarWidget.qml"), Component.PreferSynchronous)
            if (comp.status !== Component.Ready) throw new Error(comp.errorString())
            var props = {settings:{showCountdown:true}}
            if (c.moduleName) props.moduleName = c.moduleName
            widget = comp.createObject(scene.contentItem, props)
            if (!widget) throw new Error(comp.errorString())
        } else if (c.op === "api") {
            var key = c.key, allowOwnService = c.allow !== false
            api = apiComponent.createObject(null, {pluginId:key,
                _serviceLookup:function(requestedId) {
                    return allowOwnService ? host.pluginServiceFor(key, requestedId) : null
                }})
            if (!api) throw new Error("API creation failed")
            apis = apis.concat([api])
        } else if (c.op === "callback") {
            var key = api.pluginId
            api._serviceLookup = c.enabled ? function(requestedId) {
                return host.pluginServiceFor(key, requestedId)
            } : null
        } else if (c.op === "assignApi") {
            barObject.shell = api
        } else if (c.op === "injectBar") {
            widget.bar = barObject
        } else if (c.op === "service") {
            var component = Qt.createComponent(Qt.resolvedUrl(c.url || "plugin/Service.qml"), Component.PreferSynchronous)
            if (component.status !== Component.Ready) throw new Error(component.errorString())
            var service = component.createObject(null)
            if (!service) throw new Error(component.errorString())
            service.objectName = c.name
            retainedServices = retainedServices.concat([service])
            service.requestFinished.connect(function(id, op, reply) {
                host.trace("reply", {service:service.objectName, id:id, op:op, reply:reply})
            })
            service.snapshotReceived.connect(function(snapshot) {
                host.trace("snapshot", {service:service.objectName, snapshot:snapshot})
            })
            service.disconnected.connect(function(reason) {
                host.trace("disconnected", {service:service.objectName, reason:reason})
            })
        } else if (c.op === "register") {
            var map = ({})
            for (var i = 0; i < retainedServices.length; ++i)
                if (retainedServices[i].objectName === c.name) map[c.key] = retainedServices[i]
            _services = map
        } else if (c.op === "clear") {
            _services = ({})
        } else if (c.op === "identity") {
            widget.moduleName = c.key
        } else if (c.op === "click") {
            var b = button(c.text)
            if (!b || !b.enabled) throw new Error("Control disabled/missing: " + c.text)
            b.clicked()
        } else if (c.op === "offlineRequest") {
            var s = retainedServices[0]
            var id = s.next()
            trace("issued", {id:id, service:s.objectName})
        } else if (c.op === "quit") {
            Qt.quit()
        } else if (c.op !== "sample") {
            throw new Error("Unknown op: " + c.op)
        }
        Qt.callLater(function() { host.trace("done", {serial:c.serial, op:c.op, sample:sample()}) })
    }
    Timer {
        interval:50; running:true; repeat:true
        onTriggered: {
            control.reload()
            var c
            try { c = JSON.parse(control.text()) } catch (e) { return }
            if (c.serial === host.lastCommand) return
            host.lastCommand = c.serial
            try { host.execute(c) }
            catch (e) { host.trace("error", {serial:c.serial, error:String(e)} ) }
        }
    }
    Timer { interval:200; running:true; repeat:true; onTriggered:host.trace("sample", host.sample()) }
    Component.onCompleted: trace("ready", {platform:Quickshell.env("QT_QPA_PLATFORM")})
}
'''


class Lifecycle:
    def __init__(self, root, report):
        self.root, self.report = root, report
        self.daemon = self.qs = self.proxy = None
        self.logs = []
        self.serial = 0
        for name in ('home', 'config', 'state', 'cache', 'data', 'runtime', 'backend', 'front', 'walls', 'stage/services'):
            (root / name).mkdir(parents=True, mode=0o700)
        self.env = {
            'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'HOME':str(root / 'home'),
            'XDG_CONFIG_HOME':str(root / 'config'), 'XDG_STATE_HOME':str(root / 'state'),
            'XDG_CACHE_HOME':str(root / 'cache'), 'XDG_DATA_HOME':str(root / 'data'),
            'XDG_CONFIG_DIRS':str(root / 'config'), 'XDG_DATA_DIRS':'/usr/share',
            'XDG_RUNTIME_DIR':str(root / 'runtime'),
            'SWIFTWALL_CONFIG':str(root / 'config.toml'),
            'SWIFTWALL_STATE_DIR':str(root / 'state'),
            'SWIFTWALL_RUNTIME_DIR':str(root / 'front'),
            'SWIFTWALL_BACKGROUND_LINK':str(root / 'background'),
            'SWIFTWALL_OMARCHY_COMMAND':str(root / 'fake-setter'),
            'DBUS_SESSION_BUS_ADDRESS':'unix:path=' + str(root / 'no-session-bus'),
            'DBUS_SYSTEM_BUS_ADDRESS':'unix:path=' + str(root / 'no-system-bus'),
            'QT_QPA_PLATFORM':'offscreen', 'QT_QUICK_BACKEND':'software',
            'QSG_RHI_BACKEND':'software', 'QT_QUICK_CONTROLS_STYLE':'Basic',
            'QT_LOGGING_RULES':'qt.qpa.services=false', 'NO_COLOR':'1',
        }
        for i in range(3):
            (root / 'walls' / f'{i}.png').write_bytes(PNG)
        (root / 'background').symlink_to(root / 'walls/0.png')
        (root / 'config.toml').write_text('interval_seconds = 3600\nselection = "sequential"\nfolders = [' + json.dumps(str(root / 'walls')) + ']\n')
        (root / 'fake-setter').write_text('''#!/usr/bin/python3
import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
assert len(sys.argv) == 5 and sys.argv[1:4] == ["theme", "bg", "set"]
image = Path(sys.argv[4]).resolve()
assert image.parent == root / "walls"
link = Path(os.environ["SWIFTWALL_BACKGROUND_LINK"])
assert link == root / "background"
with (root / "setter.jsonl").open("a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
link.unlink(missing_ok=True)
link.symlink_to(image)
''')
        (root / 'fake-setter').chmod(0o700)
        shutil.copytree(ROOT / 'plugin', root / 'stage/plugin')
        for name in ('Ui', 'Commons'):
            shutil.copytree(INSTALLED / name, root / 'stage' / name)
        shutil.copy2(ROOT / 'tests/qml/OffscreenKeyboardPanel.qml', root / 'stage/Ui/KeyboardPanel.qml')
        shutil.copy2(INSTALLED / 'services/PluginShellApi.qml', root / 'stage/services/PluginShellApi.qml')
        shell = (INSTALLED / 'shell.qml').read_text()
        functions = '\n'.join(extract_function(shell, name) for name in ('serviceFor', 'pluginServiceFor', 'pluginOwnsTarget'))
        resolve = extract_function((INSTALLED / 'services/PluginRegistry.qml').read_text(), 'resolveEnabledId')
        report['extracted_functions'] = functions + '\n' + resolve
        report['source_sha256'] = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            ROOT / 'plugin/BarWidget.qml', ROOT / 'plugin/Panel.qml', ROOT / 'plugin/Service.qml',
            ROOT / 'plugin/SharedService.qml', INSTALLED / 'services/PluginShellApi.qml',
            INSTALLED / 'shell.qml', INSTALLED / 'services/PluginRegistry.qml')}
        (root / 'stage/shell.qml').write_text(HOST.replace('@FUNCTIONS@', functions).replace('@RESOLVE@', resolve).replace('@CONTROL@', str(root / 'control.json')))
        (root / 'control.json').write_text('{"serial":0}')

    def spawn(self, argv, name, env=None):
        log = (self.root / name).open('a')
        self.logs.append(log)
        return subprocess.Popen(argv, env=env or self.env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def events(self):
        path = self.root / 'qs.log'
        events = []
        for line in path.read_text(errors='replace').splitlines() if path.exists() else []:
            if 'LIFECYCLE ' in line:
                try:
                    events.append(json.loads(line.split('LIFECYCLE ', 1)[1]))
                except ValueError:
                    pass
        return events

    def wait(self, predicate, timeout=WAIT, after=0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = self.events()[after:]
            for event in reversed(events):
                if event['kind'] == 'error':
                    raise AssertionError('host error: ' + json.dumps(event))
                if predicate(event):
                    return event['data']
            if self.qs and self.qs.poll() is not None:
                raise AssertionError('private qs exited; see qml_log')
            time.sleep(.05)
        raise AssertionError('timed out waiting for lifecycle evidence; last events: ' + json.dumps(self.events()[-3:]))

    def command(self, op, **values):
        self.serial += 1
        command = dict(serial=self.serial, op=op, **values)
        self.report['commands'].append(command)
        temporary = self.root / 'control.new'
        temporary.write_text(json.dumps(command))
        temporary.replace(self.root / 'control.json')
        return self.wait(lambda e: e['kind'] == 'done' and e['data']['serial'] == self.serial)['sample']

    def sample(self, predicate, timeout=WAIT, after=None):
        # A fresh explicit sample prevents accidentally accepting stale state.
        if after is None:
            after = len(self.events())
        self.command('sample')
        return self.wait(lambda e: e['kind'] == 'sample' and predicate(e['data']), timeout, after)

    @staticmethod
    def expected_state_text(sample):
        service = next((s for s in sample['services'] if s['name'] == sample['widgetService']), None)
        if service is None:
            return 'Service unavailable'
        # Offline errors take precedence over retained running/paused/protection
        # snapshots. Status is authoritative for display only while online.
        if not service['online']:
            return service['error'] or 'Swiftwall is offline'
        status = service['status']
        paused = status.get('paused') is True
        if status.get('protective_suspension'):
            return ('Protectively suspended' + (' · Explicitly paused' if paused else '')
                    + ': ' + str(status['protective_suspension']) + ' · Resume manually when ready')
        return 'Explicitly paused' if paused else str(status.get('state') or 'Connected')

    def check(self, label, sample, condition, *, group='checks'):
        if 'widgetService' in sample:
            condition = condition and sample['stateText'] == self.expected_state_text(sample)
        self.report[group].append({'label':label, 'passed':bool(condition), 'sample':sample})
        # Full observations remain in the report; stdout keeps identity/control
        # evidence readable even when two services retain large status snapshots.
        if 'widgetService' in sample:
            evidence = {key:sample[key] for key in ('moduleName', 'apiId', 'lookup',
                'widgetService', 'panelService', 'stateText', 'panelState',
                'editable', 'pauseEnabled', 'nextEnabled', 'seconds')}
        else:
            evidence = sample
        print(('PASS: ' if condition else 'FAIL: ') + label + ' ' + json.dumps(evidence), flush=True)
        if not condition:
            raise AssertionError(label)

    def unavailable(self, label, *, group='checks'):
        s = self.sample(lambda s: s['panelFound'] and s['widgetService'] is None)
        self.check(label, s, s['panelService'] is None and s['stateText'] == 'Service unavailable'
                   and s['panelState'] == 'Service unavailable' and not s['editable']
                   and not s['pauseEnabled'] and not s['nextEnabled'], group=group)
        return s

    def bound(self, label, name='A', *, group='checks'):
        s = self.sample(lambda s: s['widgetService'] == name and s['panelService'] == name and s['editable'] and s['nextEnabled'])
        self.check(label, s, s['lookup'] == name and s['pauseEnabled'] and bool(s['draftRevision']), group=group)
        return s

    def direct_status(self):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(self.root / 'backend/control.sock'))
            client.sendall(b'{"v":1,"id":"lifecycle-verification","op":"status"}\n')
            with client.makefile('rb') as stream:
                return json.loads(stream.readline(1048577))

    def start_daemon(self):
        env = dict(self.env, SWIFTWALL_RUNTIME_DIR=str(self.root / 'backend'))
        self.daemon = self.spawn([str(ROOT / 'target/release/swiftwall'), 'daemon'], 'daemon.log', env)
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                raise AssertionError('private daemon exited')
            try:
                s = self.direct_status()
                if s.get('ok') and s['status'].get('eligible_images') == 3 and not s['status'].get('scanning'):
                    return s
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        raise AssertionError('private daemon startup timeout')

    def setup(self, identity=PRODUCTION, injected=None):
        self.command('reset')
        self.command('widget', **({'moduleName':injected} if injected else {}))
        self.command('api', key=identity)

    def run_fresh_preview(self):
        # Production URLs have already been loaded in this engine. Do not
        # rewrite them or claim a stale-source cache differential here.
        parent = self.root / 'stage/previews'
        parent.mkdir(mode=0o700)
        preview = stage_preview(ROOT, parent, self.root / 'front')
        identity, bundle = preview['id'], preview['bundle']
        widget_url = (bundle / 'plugin/BarWidget.qml').as_uri()
        service_url = (bundle / 'plugin/Service.qml').as_uri()
        self.report['fresh_preview'] = {
            'id':identity, 'bundle':str(bundle), 'socket':str(preview['socket']),
            'widget_url':widget_url, 'service_url':service_url,
            'production_urls_already_loaded':True,
            'cache_scope':'Fresh bundle URL lifecycle only; stale preexisting production source/type differential left to cache agent.',
            'sha256':{str(p.relative_to(bundle)):hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in bundle.rglob('*') if p.is_file()},
        }
        self.command('reset')
        self.command('widget', url=widget_url, moduleName=identity)
        self.command('api', key=identity)
        self.command('assignApi')
        self.command('injectBar')
        self.unavailable('fresh generated-ID widget before service registration', group='preview_checks')
        self.command('service', url=service_url, name='preview')
        self.sample(lambda s: any(r['name'] == 'preview' and r['online'] for r in s['services']))
        self.command('register', key=identity, name='preview')
        sample = self.bound('fresh preview scoped service and panel recover on late registration', 'preview', group='preview_checks')
        service = next(r for r in sample['services'] if r['name'] == 'preview')
        expected_widget = (ROOT / 'plugin/BarWidget.qml').read_text().replace(PRODUCTION, identity)
        self.check('fresh bundle has matching generated identity, new URLs and pinned private socket', sample,
                   sample['moduleName'] == sample['apiId'] == identity and identity != PRODUCTION
                   and widget_url != (self.root / 'stage/plugin/BarWidget.qml').as_uri()
                   and (bundle / 'plugin/BarWidget.qml').read_text() == expected_widget
                   and json.loads((bundle / 'manifest.json').read_text())['id'] == identity
                   and service['socket'] == str(preview['socket']), group='preview_checks')
        self.command('clear')
        self.unavailable('fresh preview registry removal clears service and disables controls', group='preview_checks')

    def run(self):
        try:
            self.proxy = Proxy(self.root / 'front/control.sock', self.root / 'backend/control.sock')
            self.qs = self.spawn([shutil.which('dbus-run-session'), '--', shutil.which('qs'), '--no-color', '--path', str(self.root / 'stage')], 'qs.log')
            self.wait(lambda e: e['kind'] == 'ready')
            # Genuine early-null binding: API+bar+widget exist before Service.qml.
            self.setup()
            self.command('assignApi')
            self.command('injectBar')
            self.unavailable('widget/API/bar before service: clear unavailable controls')
            self.command('service', name='A')
            self.command('register', key=PRODUCTION, name='A')
            s = self.sample(lambda s: s['widgetService'] == 'A' and s['panelService'] == 'A')
            initial_error = s['services'][0]['error']
            socket_error = 'Cannot connect to Swiftwall at ' + str(self.root / 'front/control.sock')
            self.check('early-null binding recovers with offline Connecting/error text, daemon absent', s,
                       not s['services'][0]['online'] and initial_error in ('Connecting to Swiftwall', socket_error)
                       and s['stateText'] == initial_error and s['panelState'] == initial_error
                       and not s['editable'] and not s['pauseEnabled'] and not s['nextEnabled'])
            self.report['offline_initial_sample'] = s
            before = len(self.events())
            self.command('offlineRequest')
            reply = self.wait(lambda e: e['kind'] == 'reply' and e['data']['op'] == 'next', after=before)
            self.check('offline action returns explicit unavailable', reply, reply['reply'].get('code') == 'unavailable')
            self.report['daemon_start'] = self.start_daemon()
            first = self.bound('late private daemon handshake/subscription enables injected panel')
            instance = next(s['instance'] for s in first['services'] if s['name'] == 'A')
            baseline = self.sample(lambda s: s['ticking'] and s['seconds'] > 0 and not s['services'][0]['busy'])
            time.sleep(2.3)
            later = self.command('sample')
            self.check('visible local countdown advances without status polling', later,
                       1 <= baseline['seconds'] - later['seconds'] <= 4
                       and baseline['services'][0]['serial'] == later['services'][0]['serial'])
            for text, paused in (('Pause', True), ('Resume', False), ('Next', None)):
                before = len(self.events())
                self.command('click', text=text)
                op = text.lower()
                reply = self.wait(lambda e: e['kind'] == 'reply' and e['data']['op'] == op, after=before)
                self.check('actual panel ' + text + ' reply', reply, reply['reply'].get('ok'))
                if paused is not None:
                    state = self.sample(lambda s: s['services'][0]['status'].get('paused') is paused and s['editable'])
                    self.check('subscription updates ' + op, state,
                               self.direct_status()['status']['paused'] is paused and (not state['ticking'] if paused else state['ticking']))
                else:
                    expected = str(self.root / 'walls/1.png')
                    state = self.sample(lambda s: s['services'][0]['status'].get('observed_wallpaper') == expected and not s['services'][0]['status'].get('applying') and s['editable'])
                    self.check('next reaches only validated fake setter', state, (self.root / 'background').resolve() == self.root / 'walls/1.png')
            before = len(self.events())
            stop(self.daemon)
            self.daemon = None
            offline = self.sample(lambda s: not s['services'][0]['online'] and s['services'][0]['error'] == socket_error, after=before)
            self.check('daemon loss shows socket-path error instead of stale running and disables controls', offline,
                       offline['stateText'] == socket_error and offline['panelState'] == socket_error
                       and offline['services'][0]['status'].get('state') == 'running'
                       and not offline['editable'] and not offline['pauseEnabled'] and not offline['nextEnabled'])
            self.report['offline_text_regression'] = {
                'passed':True, 'initial_offline':self.report['offline_initial_sample'],
                'after_disconnect':offline,
                'expectation':'Offline text is the service error (or offline fallback), never retained running/paused/protection text.',
                'protection_limit':'No real protective-suspension event induced; online protection formatting is checked if present in observed status.',
            }
            self.report['daemon_restart'] = self.start_daemon()
            reconnected = self.sample(lambda s: s['editable'] and s['services'][0]['online'] and s['services'][0]['instance'] != instance)
            self.check('same widget/API/service reconnects to new daemon instance', reconnected, reconnected['panelService'] == 'A')

            # Whole registry replacement keeps the same widget, API and bar.
            self.command('service', name='B')
            self.sample(lambda s: len(s['services']) == 2 and s['services'][1]['online'])
            self.command('register', key=PRODUCTION, name='B')
            self.bound('reload map swaps A to newly created B without reinjection', 'B')
            self.command('clear')
            self.unavailable('registry removal clears widget and panel service')
            self.command('register', key=PRODUCTION, name='A')
            self.bound('registry restoration recovers same binding')

            # Cross service availability with API-before/after bar injection.
            for service_first in (False, True):
                for api_first in (False, True):
                    label = f'service_first={service_first}, API_before_bar={api_first}'
                    self.command('reset')
                    if service_first:
                        self.command('register', key=PRODUCTION, name='A')
                    self.command('widget')
                    if api_first:
                        self.command('api', key=PRODUCTION)
                        self.command('assignApi')
                        self.command('injectBar')
                    else:
                        self.command('injectBar')
                        self.unavailable(label + ': bar injected without API')
                        self.command('api', key=PRODUCTION)
                        self.command('assignApi')
                    if not service_first:
                        self.unavailable(label + ': service not yet registered')
                        self.command('register', key=PRODUCTION, name='A')
                    self.bound(label + ': bound')
                    self.command('api', key=PRODUCTION, allow=False)
                    self.command('assignApi')
                    self.unavailable(label + ': replacement API denies own service')
                    self.command('api', key=PRODUCTION)
                    self.command('assignApi')
                    self.bound(label + ': replacement API restores service')

            self.setup()
            self.command('api', key=PRODUCTION, allow=False)
            self.command('assignApi')
            self.command('api', key=PRODUCTION)
            self.command('assignApi')
            self.command('register', key=PRODUCTION, name='A')
            self.command('injectBar')
            self.bound('API replaced before bar injection')
            self.command('callback', enabled=False)
            self.unavailable('same capability QObject with null _serviceLookup returns null')
            self.command('callback', enabled=True)
            self.bound('same QObject dynamic var _serviceLookup closure replacement recovers')

            # Exact identity checking: local manifest without clone metadata.
            self.setup(identity=LOCAL)
            self.command('register', key=LOCAL, name='A')
            self.command('assignApi')
            self.command('injectBar')
            mismatch = self.unavailable('local scoped identity + unchanged production moduleName is denied')
            self.check('identity mismatch is capability denial, not lost binding', mismatch, mismatch['lookup'] is None and mismatch['apiId'] == LOCAL and mismatch['moduleName'] == PRODUCTION)
            self.report['identity_scope_observation'] = {
                'sequence':'Register online A under local id, scope API to local, leave widget moduleName at production id: lookup is null. Inject moduleName=local: lookup and panel recover.',
                'interpretation':'Expected capability denial for mismatched ids without clonedFrom metadata, not a stale binding.',
                'mismatched_sample':mismatch,
            }
            self.command('identity', key=LOCAL)
            self.bound('host moduleName=local injection recovers scoped lookup')
            self.setup(identity=LOCAL, injected=LOCAL)
            self.command('assignApi')
            self.command('injectBar')
            self.unavailable('matching local identity before service registration')
            self.command('register', key=LOCAL, name='A')
            self.bound('matching local preview identity late-map recovery')
            self.command('clear')
            self.unavailable('local preview returns clearly to unavailable')
            assert len(self.report['checks']) == 41, 'retain the original 41 lifecycle checks'
            self.run_fresh_preview()
            self.report['final_status'] = self.direct_status()
            self.report['binding_lifecycle_bug_reproduced'] = False
        finally:
            stop(self.qs)
            stop(self.daemon)
            if self.proxy:
                self.proxy.close()
                self.report['traffic'] = self.proxy.records
            for log in self.logs:
                log.close()
            for name in ('qs', 'daemon'):
                path = self.root / (name + '.log')
                self.report['qml_log' if name == 'qs' else 'daemon_log'] = path.read_text(errors='replace') if path.exists() else ''
            self.report['events'] = self.events()
            setter = self.root / 'setter.jsonl'
            self.report['setter_calls'] = [json.loads(line) for line in setter.read_text().splitlines()] if setter.exists() else []
        requests = [r['frame'] for r in self.report['traffic'] if r.get('direction') == 'request']
        replies = [r['frame'] for r in self.report['traffic'] if r.get('direction') == 'reply']
        operations = [r.get('op') for r in requests]
        self.report['wire_operations'] = operations
        assert all(op in operations for op in ('hello', 'subscribe', 'status', 'pause', 'resume', 'next'))
        instances = {r.get('instance') for r in replies if r.get('event') == 'snapshot'}
        assert len(instances - {None}) >= 2, 'missing restart subscription wire evidence'
        assert len(self.report['setter_calls']) == 1, 'unexpected setter activity'
        self.report['wire_snapshot_instances'] = sorted(instances - {None})
        self.report['outcome'] = 'passed'
        self.report['cleanup'] = {'qs_session_returncode':self.qs.returncode,
                                  'daemon_returncode':self.daemon.returncode,
                                  'proxy_threads_joined':True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='retain full stdout-equivalent observations, QML logs and transport evidence')
    args = parser.parse_args()
    for tool in ('qs', 'dbus-run-session'):
        if not shutil.which(tool):
            parser.error(tool + ' required')
    binary = ROOT / 'target/release/swiftwall'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('existing target/release/swiftwall executable required; no build performed')
    if args.report and args.report.resolve().is_relative_to(ROOT):
        parser.error('--report must be outside the repository (new script is the only repo write)')
    report = {'outcome':'incomplete', 'binary':str(binary),
              'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
              'commands':[], 'checks':[], 'preview_checks':[], 'findings':[],
              'limits':['Private host, not the complete installed shell/bar loader.',
                        'Controlled registry has no clone metadata; exact installed identity resolver is extracted.',
                        'Bar geometry/tooltip callbacks are fixtures; service lookup is the genuine scoped QObject.',
                        'Only staged KeyboardPanel surface shimmed; no compositor, focus, portal or full GUI surface tested.',
                        'Fresh preview uses stage_preview identity/socket transformations on disposable copies only.',
                        'No stale-production-source cache reproduction; that scenario is left to the cache agent.']}
    try:
        with tempfile.TemporaryDirectory(prefix='sw-life-') as temporary:
            probe = Lifecycle(Path(temporary), report)
            probe.run()
        print('PASS: private widget lifecycle; checks=' + str(len(report['checks'])) + '; fake setter calls=' + str(len(report['setter_calls'])), flush=True)
        print('PASS: fresh stage_preview lifecycle; checks=' + str(len(report['preview_checks'])) + '; id=' + report['fresh_preview']['id'], flush=True)
        print('WIRE: ' + json.dumps(report['wire_operations']), flush=True)
        print('LIMITS: ' + ' '.join(report['limits']), flush=True)
    except Exception as error:
        report['outcome'] = 'failed'
        report['error'] = str(error)
        print(report.get('qml_log', ''), flush=True)
        print('FAIL: ' + str(error), flush=True)
        raise
    finally:
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + '\n')
            print('Evidence: ' + str(args.report), flush=True)


if __name__ == '__main__':
    main()
