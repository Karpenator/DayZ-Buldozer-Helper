/*
 * capture.js — Frida-агент: перехват трафика канала Buldozer <-> Terrain Builder.
 *
 * Вешается на kernel32!CreateFileW / CreateNamedPipeW / ConnectNamedPipe (чтобы узнать
 * имена каналов) и на kernel32!ReadFile / !WriteFile (чтобы записать содержимое). С
 * флагом nt=true дополнительно вешаются ntdll!NtReadFile / !NtWriteFile.
 *
 * Пишем только то, что относится к каналам (GetFileType == FILE_TYPE_PIPE): без фильтра
 * TB и DayZ заваливают перехват обычными файлами (логи, конфиги, pbo).
 *
 * Загружается из frida_capture.py; отдельно запускать не нужно.
 */
'use strict';

const CONFIG = {
    nt: false,             // дублировать перехват на уровне ntdll
    hexLimit: 0,           // 0 = отдавать всё; иначе обрезать запись до N байт
    opensOnly: false,      // только открытия каналов, без содержимого
    resolveNames: true,    // узнавать имя уже открытых каналов (NtQueryObject)
};

const FILE_TYPE_PIPE = 3;

function exportOf(name) {
    try {
        if (typeof Module.getGlobalExportByName === 'function') {
            return Module.getGlobalExportByName(name);
        }
    } catch (e) { /* нет такого API в этой версии Frida */ }
    try { return Module.getExportByName(null, name); } catch (e) { /* старые версии */ }
    try { return Module.findExportByName(null, name); } catch (e) { /* совсем старые */ }
    return null;
}

const _getFileType = (() => {
    const addr = exportOf('GetFileType');
    if (addr === null) return null;
    return new NativeFunction(addr, 'uint32', ['pointer']);
})();

const typeCache = new Map();
function isPipe(handle) {
    if (handle.isNull()) return false;
    const key = handle.toString();
    let cached = typeCache.get(key);
    if (cached === undefined) {
        cached = _getFileType === null ? false : (_getFileType(handle) === FILE_TYPE_PIPE);
        typeCache.set(key, cached);
    }
    return cached;
}

const names = new Map();          // handle -> имя канала
function remember(handle, wideNamePtr) {
    try {
        const name = wideNamePtr.isNull() ? '' : wideNamePtr.readUtf16String();
        if (name) names.set(handle.toString(), name);
        return name;
    } catch (e) {
        return '';
    }
}

function pathOf(handle) {
    return names.get(handle.toString()) || '';
}

// --- имя уже открытого канала -------------------------------------------------
// Хуки открытий видят только то, что открыли после нас. Канал, открытый раньше
// (например, TB держал соединение с уже запущенным Buldozer), остаётся безымянным:
// в дампе видно трафик, но непонятно, какой это канал. NtQueryObject решает это.
const _ntQueryObject = (() => {
    const addr = exportOf('NtQueryObject');          // есть только в ntdll
    if (addr === null) return null;
    return new NativeFunction(addr, 'int32',
        ['pointer', 'uint32', 'pointer', 'uint32', 'pointer']);
})();

const resolving = new Set();       // имена, которые выясняются прямо сейчас
const hopeless = new Set();        // для этих дескрипторов имя не узнать
const MAX_RESOLVE = 256;

function normalisePipeName(raw) {
    const name = raw || '';
    const marker = '\\device\\namedpipe\\';
    if (name.toLowerCase().indexOf(marker) === 0) {
        // \Device\NamedPipe\1A041E,8EDB -> \\.\pipe\1A041E,8EDB (как в -connect=pipe\...)
        return '\\\\.\\pipe\\' + name.slice(marker.length);
    }
    if (name.indexOf('\\??\\') === 0) return name.slice(4);
    return name;
}

function resolveName(handle) {
    if (_ntQueryObject === null || !CONFIG.resolveNames) return;
    const key = handle.toString();
    if (names.has(key) || resolving.has(key) || hopeless.has(key)) return;
    if (resolving.size >= MAX_RESOLVE) return;
    resolving.add(key);
    // Вызываем из своего потока (setTimeout у Frida), а не из потока приложения:
    // NtQueryObject на канале с висящей операцией умеет блокироваться надолго.
    setTimeout(() => {
        let found = false;
        try {
            const info = Memory.alloc(2048);
            const status = _ntQueryObject(handle, 1 /* ObjectNameInformation */, info, 2048,
                                          Memory.alloc(4));
            if (status === 0) {
                const bytes = info.readU16();
                const buffer = Process.pointerSize === 8
                    ? info.add(8).readPointer() : info.add(4).readPointer();
                const chars = Math.min(bytes, 2048) / 2;
                if (!buffer.isNull() && chars > 1) {
                    const name = normalisePipeName(buffer.readUtf16String(chars) || '');
                    if (name) {
                        names.set(key, name);
                        emit('name', handle, null, {api: 'NtQueryObject', name: name});
                        found = true;
                    }
                }
            }
        } catch (e) {
            // имя не обязательно: просто оставим дескриптор без подписи
        }
        resolving.delete(key);
        if (!found) hopeless.add(key);
    }, 0);
}

function ensureName(handle) {
    if (names.has(handle.toString())) return;
    resolveName(handle);
}

let counter = 0;
function emit(kind, handle, data, extra) {
    const record = {
        kind: kind,
        seq: counter++,
        handle: handle.isNull() ? 0 : handle.toInt32(),
        path: pathOf(handle),
    };
    if (extra) {
        for (const key in extra) record[key] = extra[key];
    }
    let payload = null;
    if (data !== null && data.byteLength > 0) {
        let bytes = data;
        if (CONFIG.hexLimit > 0 && bytes.byteLength > CONFIG.hexLimit) {
            record.truncated_to = CONFIG.hexLimit;
            record.full_length = bytes.byteLength;
            bytes = bytes.slice(0, CONFIG.hexLimit);
        }
        payload = bytes;
    }
    send(record, payload);
}

function attachIO(name, hook, moduleName) {
    let addr = null;
    if (moduleName) {
        try {
            const mod = Process.findModuleByName(moduleName);
            if (mod !== null) addr = mod.findExportByName(name);
        } catch (e) { addr = null; }
    }
    if (addr === null) addr = exportOf(name);
    if (addr === null) {
        send({kind: 'warn', text: 'export не найден: ' + (moduleName ? moduleName + '!' : '') + name});
        return;
    }
    const key = addr.toString();
    if (attachedAddrs.has(key)) return;   // kernel32.forward == kernelbase!impl: не вешаем дважды
    attachedAddrs.add(key);
    Interceptor.attach(addr, hook);
}
const attachedAddrs = new Set();

// --- открытия каналов -------------------------------------------------------
attachIO('CreateFileW', {
    onEnter(args) {
        this.name = args[0].readUtf16String() || '';
        this.wanted = this.name.toLowerCase().indexOf('\\pipe\\') >= 0;
    },
    onLeave(retval) {
        if (!this.wanted || retval.isNull() || retval.toInt32() === 0) return;
        remember(retval, Memory.allocUtf16String(this.name));
        typeCache.set(retval.toString(), true);
        if (!CONFIG.opensOnly) {
            emit('open', retval, null, {api: 'CreateFileW', name: this.name});
        }
    },
});

attachIO('CreateFile2', {
    onEnter(args) {
        this.name = args[0].readUtf16String() || '';
        this.wanted = this.name.toLowerCase().indexOf('\\pipe\\') >= 0;
    },
    onLeave(retval) {
        if (!this.wanted || retval.isNull() || retval.toInt32() === 0) return;
        remember(retval, Memory.allocUtf16String(this.name));
        typeCache.set(retval.toString(), true);
        if (!CONFIG.opensOnly) {
            emit('open', retval, null, {api: 'CreateFile2', name: this.name});
        }
    },
});

attachIO('CreateNamedPipeW', {
    onEnter(args) {
        this.name = args[0].readUtf16String() || '';
    },
    onLeave(retval) {
        if (retval.isNull() || retval.toInt32() === 0) return;
        remember(retval, Memory.allocUtf16String(this.name));
        typeCache.set(retval.toString(), true);
        emit('create', retval, null, {api: 'CreateNamedPipeW', name: this.name});
    },
});

attachIO('ConnectNamedPipe', {
    onEnter(args) {
        this.handle = args[0];
    },
    onLeave(retval) {
        emit('connect', this.handle, null, {api: 'ConnectNamedPipe', ok: retval.toInt32() !== 0});
    },
});

// --- содержимое канала ------------------------------------------------------
function ioHook(api, direction) {
    return {
        onEnter(args) {
            this.handle = args[0];
            if (!isPipe(this.handle)) return;
            this.buffer = args[1];
            this.length = args[2].toInt32();
            this.writtenPtr = args[3];
            this.overlapped = args[4];
            this.captured = 0;
            if (this.length > 0 && this.length <= 16 * 1024 * 1024) {
                if (direction === 'tx') {
                    try {
                        this.data = this.buffer.readByteArray(this.length);
                    } catch (e) { this.data = null; }
                }
            }
        },
        onLeave(retval) {
            if (this.data === undefined && !this.handle) return;
            if (!isPipe(this.handle)) return;
            let n = 0;
            try {
                if (this.writtenPtr && !this.writtenPtr.isNull()) {
                    n = this.writtenPtr.readU32();
                }
            } catch (e) { n = 0; }
            let data = null;
            if (direction === 'tx') {
                data = this.data;
                if (n > 0 && n < this.length && data !== null) data = data.slice(0, n);
            } else if (n > 0 && this.length <= 16 * 1024 * 1024) {
                try {
                    data = this.buffer.readByteArray(n);
                } catch (e) { data = null; }
            }
            emit(direction, this.handle, data, {
                api: api,
                requested: this.length,
                completed: n,
                ok: retval.toInt32() !== 0,
                overlapped: this.overlapped.isNull() ? false : true,
            });
            if (!pathOf(this.handle)) ensureName(this.handle);
        },
    };
}

if (!CONFIG.opensOnly) {
    // Kernelbase: часть программ дергает ReadFile/WriteFile через IAT kernelbase
    // (та же реализация, что и kernel32!forward — дедуп по адресу выше не даст
    // двойных записей). Обе точки вешаем, чтобы не пропустить ни одну сторону.
    attachIO('WriteFile', ioHook('WriteFile', 'tx'));
    attachIO('ReadFile', ioHook('ReadFile', 'rx'));
    attachIO('WriteFile', ioHook('kernelbase!WriteFile', 'tx'), 'kernelbase.dll');
    attachIO('ReadFile', ioHook('kernelbase!ReadFile', 'rx'), 'kernelbase.dll');

    // ReadFileEx/WriteFileEx (APC-завершение) — TB может ходить через них: их
    // обходятся те же NtReadFile/NtWriteFile, но дамп не видел их из TB. Здесь
    // данные читаются в callback-обёртке (завершение чтения).
    const liveCallbacks = [];
    function wrapCompletion(original, handle, buffer, length) {
        const cb = new NativeCallback(function (err, n, ov) {
            if (isPipe(handle)) {
                let data = null;
                if (n > 0 && n <= 16 * 1024 * 1024) {
                    try { data = buffer.readByteArray(n); } catch (e) { data = null; }
                }
                emit('rx', handle, data, {
                    api: 'ReadFileEx', requested: length, completed: n, ok: err === 0,
                });
            }
            if (original && !original.isNull()) {
                try {
                    const fn = new NativeFunction(original, 'void', ['uint32', 'uint32', 'pointer']);
                    fn(err, n, ov);
                } catch (e) { /* оригинал умер — не мешаем */ }
            }
        }, 'void', ['uint32', 'uint32', 'pointer']);
        liveCallbacks.push(cb);
        if (liveCallbacks.length > 512) liveCallbacks.shift();
        return cb;
    }

    attachIO('WriteFileEx', {
        onEnter(args) {
            this.handle = args[0];
            this.length = args[2].toInt32();
            this.data = null;
            if (isPipe(this.handle) && this.length > 0 && this.length <= 16 * 1024 * 1024) {
                try { this.data = args[1].readByteArray(this.length); } catch (e) { }
            }
        },
        onLeave(retval) {
            if (this.data === null && !isPipe(this.handle)) return;
            emit('tx', this.handle, this.data, {
                api: 'WriteFileEx', requested: this.length, ok: retval.toInt32() !== 0,
            });
        },
    });

    attachIO('ReadFileEx', {
        onEnter(args) {
            this.handle = args[0];
            this.length = args[2].toInt32();
            if (!isPipe(this.handle)) return;
            this.buffer = args[1];
            this.origCallback = args[4];
            // подменяем callback завершения, чтобы поймать прочитанные данные
            args[4] = wrapCompletion(this.origCallback, this.handle, this.buffer, this.length);
        },
        onLeave(retval) {
            if (!isPipe(this.handle)) return;
            if (!pathOf(this.handle)) ensureName(this.handle);
        },
    });

    // TransactNamedPipe: запись и чтение одним вызовом (сторона TB может ходить
    // через него — тогда ReadFile/WriteFile не видно вообще).
    attachIO('TransactNamedPipe', {
        onEnter(args) {
            this.handle = args[0];
            this.inLength = args[2].toInt32();
            this.outLength = args[4].toInt32();
            this.outBuffer = args[3];
            this.bytesReadPtr = args[5];
            this.data = null;
            if (isPipe(this.handle) && this.inLength > 0 && this.inLength <= 16 * 1024 * 1024) {
                try { this.data = args[1].readByteArray(this.inLength); } catch (e) { }
            }
        },
        onLeave(retval) {
            if (!isPipe(this.handle)) return;
            if (this.data !== null) {
                emit('tx', this.handle, this.data, {
                    api: 'TransactNamedPipe', requested: this.inLength, ok: retval.toInt32() !== 0,
                });
            }
            let n = 0;
            try {
                if (this.bytesReadPtr && !this.bytesReadPtr.isNull()) n = this.bytesReadPtr.readU32();
            } catch (e) { n = 0; }
            let out = null;
            if (n > 0 && this.outLength <= 16 * 1024 * 1024) {
                try { out = this.outBuffer.readByteArray(n); } catch (e) { out = null; }
            }
            if (out !== null) {
                emit('rx', this.handle, out, {
                    api: 'TransactNamedPipe', requested: this.outLength, completed: n,
                    ok: retval.toInt32() !== 0,
                });
            }
        },
    });
}

if (CONFIG.nt) {
    const iosbOffset = Process.pointerSize === 8 ? 8 : 4;   // IO_STATUS_BLOCK.Information
    function ntHook(api, direction) {
        return {
            onEnter(args) {
                this.handle = args[0];
                if (!isPipe(this.handle)) return;
                this.iosb = args[4];
                this.buffer = args[5];
                this.length = args[6].toInt32();
                this.data = null;
                if (direction === 'tx' && this.length > 0 && this.length <= 16 * 1024 * 1024) {
                    try { this.data = this.buffer.readByteArray(this.length); } catch (e) { }
                }
            },
            onLeave(retval) {
                if (!isPipe(this.handle)) return;
                let n = 0;
                try {
                    if (this.iosb && !this.iosb.isNull()) {
                        n = this.iosb.add(iosbOffset).readPointer().toInt32();
                    }
                } catch (e) { n = 0; }
                let data = this.data;
                if (direction === 'rx' && n > 0 && this.length <= 16 * 1024 * 1024) {
                    try { data = this.buffer.readByteArray(n); } catch (e) { data = null; }
                }
                emit(direction, this.handle, data, {
                    api: api, requested: this.length, completed: n,
                    status: retval.toInt32(), nt: true,
                });
            },
        };
    }
    attachIO('NtWriteFile', ntHook('NtWriteFile', 'tx'));
    attachIO('NtReadFile', ntHook('NtReadFile', 'rx'));
}

rpc.exports = {
    configure(options) {
        if (options && typeof options === 'object') {
            for (const key in CONFIG) {
                if (options[key] !== undefined) CONFIG[key] = options[key];
            }
        }
        return CONFIG;
    },
    hello() {
        return {pid: Process.id, arch: Process.arch, platform: Process.platform};
    },
};
