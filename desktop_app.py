"""Windows desktop and tray host for the existing Roc sender."""
from __future__ import annotations
import json
import codecs
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

from PySide6.QtCore import QProcess, QProcessEnvironment, QStandardPaths, QTimer, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSystemTrayIcon,
    QVBoxLayout, QWidget)
import sender

APP_NAME = 'Roc Sender'
STARTUP_NAME = 'RocSender'


def resources():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def default_settings():
    with (resources() / 'config.example.toml').open('rb') as handle:
        values = tomllib.load(handle)
    return {key: value for key, value in values.items() if key != 'dll'} | {
        'stats_interval': 10.0, 'auto_stream': False}


def validate_settings(values):
    if values['capture_mode'] not in ('loopback', 'input'):
        raise ValueError('请选择 Loopback 或录音端采集。')
    if not values['device'].strip():
        raise ValueError('请选择音频设备。')
    if type(values['chunk_frames']) is not int or values['chunk_frames'] < 1:
        raise ValueError('每块采集帧数必须为正整数。')
    if not math.isfinite(values['stats_interval']) or values['stats_interval'] <= 0:
        raise ValueError('诊断间隔必须为正数。')
    for key, scheme in (('source', 'rtp+rs8m'), ('repair', 'rs8m'), ('control', 'rtcp')):
        sender.validate_endpoint(values[key], scheme)
    return values


def launch_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def worker_command(values):
    validate_settings(values)
    dll = resources() / ('native/roc.dll' if getattr(sys, 'frozen', False) else 'build/Release/roc.dll')
    command = ([str(Path(sys.executable).with_name('RocSenderWorker.exe'))] if getattr(sys, 'frozen', False)
               else [sys.executable, str(Path(__file__).with_name('worker_entry.py'))])
    command += ['--stop-stdin', '--dll', str(dll)]
    for key in ('capture_mode', 'device', 'source', 'repair', 'control', 'chunk_frames', 'stats_interval'):
        command.extend(['--' + key.replace('_', '-'), str(values[key])])
    return command


def startup_command():
    return subprocess.list2cmdline(launch_command() + ['--tray'])


def startup_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_NAME)
            return value == startup_command()
    except FileNotFoundError:
        return False


def set_startup(enabled):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_NAME)
            except FileNotFoundError:
                pass  # Already disabled is the intended registry state.


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor('#207dff'))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 15, 15)
    painter.setBrush(QColor('white'))
    for x, height in ((14, 16), (24, 32), (34, 42), (44, 24)):
        painter.drawRoundedRect(x, (64-height)//2, 6, height, 3, 3)
    painter.end()
    return QIcon(pixmap)


class Window(QMainWindow):
    def __init__(self, settings_path=None, enumerate_devices=True):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(740, 700)
        self.settings_path = settings_path or Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation)) / 'settings.json'
        self.quitting = False
        self.stop_requested = False
        self.output_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.output_pending = ''
        self.monitor_data = None
        self.monitor_received_at = None
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONUNBUFFERED', '1')
        env.insert('PYTHONIOENCODING', 'utf-8')
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.started.connect(lambda: self.set_state('发送进程运行中'))
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.stop_timer = QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self.stop_timeout)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        title = QLabel('Roc Sender')
        title.setStyleSheet('font-size:26px;font-weight:600;color:#207dff')
        layout.addWidget(title)
        layout.addWidget(QLabel('Windows 音频 → 局域网 Roc 播放器'))
        self.status = QLabel('已停止')
        layout.addWidget(self.status)
        monitoring = QGroupBox('链路监控')
        monitor_layout = QVBoxLayout(monitoring)
        self.local_status = QLabel('本地采集：未运行')
        self.remote_status = QLabel('接收反馈：未运行')
        self.latency_status = QLabel('Roc 端到端延迟：未知（估算依赖两端时钟同步）')
        self.playback_status = QLabel('实际播放：未知；RTCP 不能确认播放器出声或线程是否阻塞')
        for label in (self.local_status, self.remote_status, self.latency_status, self.playback_status):
            label.setWordWrap(True)
            monitor_layout.addWidget(label)
        layout.addWidget(monitoring)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(1000)
        self.monitor_timer.timeout.connect(self.check_monitor_freshness)
        self.monitor_timer.start()
        self.config_group = QGroupBox('音频与接收端')
        form = QFormLayout(self.config_group)
        self.mode = QComboBox()
        self.mode.addItem('输出设备 Loopback', 'loopback')
        self.mode.addItem('录音输入设备', 'input')
        form.addRow('采集模式', self.mode)
        self.device = QComboBox()
        self.device.setMinimumContentsLength(35)
        device_row = QHBoxLayout()
        device_row.addWidget(self.device, 1)
        refresh = QPushButton('刷新设备')
        refresh.clicked.connect(self.refresh_devices)
        device_row.addWidget(refresh)
        form.addRow('音频设备', device_row)
        self.endpoints = {}
        for key, label in (('source','音频地址'), ('repair','纠错地址'), ('control','控制地址')):
            field = QLineEdit()
            self.endpoints[key] = field
            form.addRow(label, field)
        self.chunk = QSpinBox()
        self.chunk.setRange(1, 192000)
        form.addRow('每块采集帧数', self.chunk)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.1, 3600)
        self.interval.setSuffix(' 秒')
        form.addRow('诊断间隔', self.interval)
        layout.addWidget(self.config_group)
        self.startup = QCheckBox('登录 Windows 时启动到托盘')
        self.startup.setChecked(startup_enabled())
        self.startup.toggled.connect(self.change_startup)
        layout.addWidget(self.startup)
        self.auto_stream = QCheckBox('打开应用时自动开始发送')
        layout.addWidget(self.auto_stream)
        row = QHBoxLayout()
        self.start_button = QPushButton('开始发送')
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton('停止发送')
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        save = QPushButton('保存配置')
        save.clicked.connect(self.save_clicked)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        row.addWidget(save)
        layout.addLayout(row)
        layout.addWidget(QLabel('运行诊断（采集／发送状态与错误）'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        layout.addWidget(self.log, 1)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu(self)
        menu.addAction('显示窗口', self.show_window)
        self.tray_start = menu.addAction('开始发送', self.start)
        self.tray_stop = menu.addAction('停止发送', self.stop)
        self.tray_stop.setEnabled(False)
        menu.addSeparator()
        menu.addAction('退出', self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window() if reason in (
            QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self.tray.setToolTip('Roc Sender · 已停止')
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        values = default_settings()
        if self.settings_path.exists():
            with self.settings_path.open(encoding='utf-8') as handle:
                values.update(json.load(handle))
            validate_settings(values)
        self.mode.setCurrentIndex(self.mode.findData(values['capture_mode']))
        self.device.addItem(values['device'])
        for key, field in self.endpoints.items():
            field.setText(values[key])
        self.chunk.setValue(values['chunk_frames'])
        self.interval.setValue(values['stats_interval'])
        self.auto_stream.setChecked(values['auto_stream'])
        self.mode.currentIndexChanged.connect(self.refresh_devices)
        if enumerate_devices:
            self.refresh_devices()
        if values['auto_stream']:
            QTimer.singleShot(0, self.start)

    def values(self):
        return validate_settings(dict(capture_mode=self.mode.currentData(), device=self.device.currentText(),
            chunk_frames=self.chunk.value(), stats_interval=self.interval.value(),
            auto_stream=self.auto_stream.isChecked(), **{key: field.text().strip() for key, field in self.endpoints.items()}))

    def save(self):
        values = self.values()
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.settings_path)
        return values

    def save_clicked(self):
        try:
            self.save()
        except (OSError, ValueError) as error:
            self.report_error(str(error))
        else:
            self.log.appendPlainText('配置已保存。')

    def refresh_devices(self):
        import pyaudiowpatch as audio
        previous = self.device.currentText()
        try:
            with audio.PyAudio() as pa:
                host = pa.get_host_api_info_by_type(audio.paWASAPI)['index']
                devices = sender.capture_devices([pa.get_device_info_by_index(i) for i in range(pa.get_device_count())], host, self.mode.currentData())
        except Exception as error:
            self.report_error('无法枚举音频设备：' + str(error))
            return
        self.device.clear()
        self.device.addItems([device['name'] for device in devices])
        matches = [i for i, device in enumerate(devices) if previous.casefold() in device['name'].casefold()]
        if previous and len(matches) == 1:
            self.device.setCurrentIndex(matches[0])
        else:
            self.device.setCurrentIndex(-1)
            self.device.setPlaceholderText('请选择设备' if devices else '没有可用设备')

    def change_startup(self, enabled):
        try:
            set_startup(enabled)
        except OSError as error:
            self.startup.blockSignals(True)
            self.startup.setChecked(not enabled)
            self.startup.blockSignals(False)
            self.report_error('无法更新 Windows 启动设置：' + str(error))

    def start(self):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        try:
            command = worker_command(self.save())
        except (OSError, ValueError) as error:
            self.report_error(str(error))
            return
        self.stop_requested = False
        self.output_decoder.reset()
        self.output_pending = ''
        self.clear_monitor('等待采集数据', '等待 RTCP 接收反馈')
        self.monitor_received_at = time.monotonic()
        self.config_group.setEnabled(False)
        self.start_button.setEnabled(False)
        self.tray_start.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.tray_stop.setEnabled(True)
        self.set_state('正在启动')
        self.process.start(command[0], command[1:])

    def stop(self):
        if self.process.state() == QProcess.ProcessState.NotRunning:
            if self.quitting:
                QApplication.quit()
            return
        self.stop_requested = True
        self.set_state('正在停止并排空音频队列')
        self.process.write(b'stop\n')
        self.process.closeWriteChannel()
        self.stop_timer.start(10000)

    def stop_timeout(self):
        if self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.log.appendPlainText('WARNING: 发送进程在 10 秒内未正常停止，强制结束；可能丢失待发送音频。')
        self.tray.showMessage(APP_NAME, '发送进程停止超时，已强制结束。', QSystemTrayIcon.MessageIcon.Warning)
        self.process.kill()

    def read_output(self):
        self.consume_output(bytes(self.process.readAllStandardOutput()))

    def consume_output(self, data, final=False):
        self.output_pending += self.output_decoder.decode(data, final=final)
        lines = self.output_pending.split('\n')
        self.output_pending = lines.pop()
        if final and self.output_pending:
            lines.append(self.output_pending)
            self.output_pending = ''
        for line in lines:
            line = line.rstrip('\r')
            if line.startswith('ROC_MONITOR '):
                try:
                    self.update_monitor(json.loads(line[len('ROC_MONITOR '):]))
                except (ValueError, KeyError, TypeError) as error:
                    self.clear_monitor('监控数据无效', '未知，不能验证接收状态')
                    self.log.appendPlainText('ERROR: 无法读取链路监控：' + str(error))
            elif line:
                self.log.appendPlainText(line)

    def update_monitor(self, data):
        local, remote = data['local'], data['remote']
        local_names = {'receiving': '正在采集 PCM', 'silent': '采到静音 PCM', 'no_pcm': '未收到 PCM'}
        remote_names = {'waiting': '尚未发现 RTCP 接收反馈', 'feedback': '已发现 RTCP 接收反馈',
                        'lost': '接收反馈已消失', 'unverified': '无法验证（本地采集未推进）'}
        local_name, remote_name = local_names[local['state']], remote_names[remote['state']]
        elapsed = float(local['elapsed_s'])
        if elapsed <= 0 or not math.isfinite(elapsed):
            raise ValueError('无效监控时间窗口')
        count = int(remote['receiver_count'])
        latencies = [float(value) for value in remote['latencies_ms'] if value is not None]
        if any(not math.isfinite(value) or value <= 0 for value in latencies):
            raise ValueError('无效的延迟估算')
        previous = self.monitor_data['remote']['state'] if self.monitor_data else None
        self.local_status.setText(f"本地采集：{local_name} · {local['captured_frames'] / elapsed:.0f} 帧/秒 · "
                                  f"已提交 {local['sent_frames']} 帧 · 峰值 {local['peak']:.4f} · 队列 {local['queue_blocks']} 块")
        self.remote_status.setText(f'接收反馈：{remote_name}（Roc 报告 {count} 个接收端）')
        self.remote_status.setStyleSheet('color:#b42318' if remote['state'] == 'lost' else 'color:#795500' if remote['state'] != 'feedback' else '')
        estimates = ' / '.join(f'{value:.1f} ms' for value in latencies) if latencies else '未知'
        self.latency_status.setText(f'Roc 端到端延迟：{estimates}（估算依赖两端时钟同步，不含实际出声确认）')
        self.monitor_data = data
        self.monitor_received_at = time.monotonic()
        if remote['state'] == 'lost' and previous != 'lost':
            self.tray.showMessage(APP_NAME, 'RTCP 接收反馈已消失；本地发送不代表远端仍在播放。', QSystemTrayIcon.MessageIcon.Warning)

    def clear_monitor(self, local='未运行', remote='未运行'):
        self.monitor_data = None
        self.monitor_received_at = None
        self.local_status.setText('本地采集：' + local)
        self.remote_status.setText('接收反馈：' + remote)
        self.remote_status.setStyleSheet('')
        self.latency_status.setText('Roc 端到端延迟：未知（估算依赖两端时钟同步）')

    def check_monitor_freshness(self):
        if self.process.state() == QProcess.ProcessState.NotRunning or self.monitor_received_at is None:
            return
        if time.monotonic() - self.monitor_received_at > 3:
            self.clear_monitor('监控更新已中断', '未知，监控数据已过期')

    def set_state(self, state):
        self.status.setText(state)
        self.tray.setToolTip('Roc Sender · ' + state)

    def reset_controls(self):
        self.config_group.setEnabled(True)
        self.start_button.setEnabled(True)
        self.tray_start.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.tray_stop.setEnabled(False)

    def finished(self, code, status):
        self.stop_timer.stop()
        self.read_output()
        self.consume_output(b'', final=True)
        self.clear_monitor()
        self.reset_controls()
        failed = code != 0 or status == QProcess.ExitStatus.CrashExit
        self.set_state('发送失败，请查看诊断' if failed else '已停止')
        if failed:
            self.tray.showMessage(APP_NAME, '发送进程异常退出，请打开窗口查看诊断。', QSystemTrayIcon.MessageIcon.Critical)
            if not self.quitting:
                self.show_window()
        if self.quitting:
            QApplication.quit()

    def process_error(self, error):
        self.log.appendPlainText('ERROR: ' + self.process.errorString())
        if error == QProcess.ProcessError.FailedToStart:
            self.clear_monitor()
            self.reset_controls()
            self.set_state('启动失败')
            self.report_error(self.process.errorString())
            if self.quitting:
                QApplication.quit()

    def report_error(self, message):
        self.log.appendPlainText('ERROR: ' + message)
        QMessageBox.critical(self, APP_NAME, message)

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.tray.isVisible():
            event.ignore()
            self.hide()
        else:
            event.ignore()
            self.quit_app()

    def changeEvent(self, event):
        super().changeEvent(event)
        if self.isMinimized() and self.tray.isVisible():
            QTimer.singleShot(0, self.hide)

    def quit_app(self):
        self.quitting = True
        self.stop()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName('RocSender')
    app.setApplicationName('RocSender')
    app.setQuitOnLastWindowClosed(False)
    name = 'roc-sender-desktop-' + os.environ.get('USERNAME', 'user')
    socket = QLocalSocket()
    socket.connectToServer(name)
    if socket.waitForConnected(500):
        socket.write(b'show')
        socket.waitForBytesWritten(500)
        return 0
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        QMessageBox.critical(None, APP_NAME, '无法建立应用单实例服务：' + server.errorString())
        return 1
    try:
        window = Window()
    except Exception as error:
        QMessageBox.critical(None, APP_NAME, '应用启动失败：' + str(error))
        return 1
    def activate():
        connection = server.nextPendingConnection()
        if connection:
            connection.disconnectFromServer()
            connection.deleteLater()
        window.show_window()
    server.newConnection.connect(activate)
    if '--tray' not in sys.argv or not window.tray.isVisible():
        window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
