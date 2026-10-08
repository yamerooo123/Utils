AI SLOP

# Dissecting DexProtector in `com.dexprotector.detector.envchecks`: From a Custom Loader to a Clean-Snapshot Bypass

> Scope note: This write-up documents the reverse-engineering process in a lab/CTF sandbox. The goal is to understand how `libdexprotector.so`, its hidden native image, and the checks affected by Frida work, then build a minimally invasive bypass to observe runtime behavior.

## Introduction

DexProtector does more than just “rename classes” or “encrypt strings.” In this sample, it sets up an entire native bootstrap chain:

```text
ProtectedApplication.attachBaseContext()
        ↓ System.loadLibrary("dexprotector")
libdexprotector.so .init_array
        ↓ decrypt/decompress/map hidden image
hidden lib / libdp-like image
        ↓ hidden JNI entry sub_4E354(JavaVM*)
RASP + key derivation + asset decrypt + RegisterNatives
        ↓
ProtectedApplication.onCreate() / ye()
```

The most interesting part is that many checks inspect not only the Android environment but also the actual bytes of the hidden native image. Because Frida inline hooks modify instructions, hooking at the wrong time can poison the key/context, causing asset decryption to fail or ART to crash.

This article follows a “reverse-engineering diary” style: each step covers observations, hypotheses, tests, and conclusions.

---

## 1. Finding the Right Entry Point: Do Not Look Only at `System.loadLibrary`

In Java/JADX, the app uses `ProtectedApplication`. An ordinary app would not have this class; it is an Application wrapper inserted by the protector.

`ProtectedApplication.attachBaseContext()` invokes native loading, eventually bringing `libdexprotector.so` into the process. However, if you hook Java too early, ART/GC may traverse Frida frames or trampolines while the hidden native image is still initializing, producing difficult-to-diagnose crashes. A more stable approach is therefore to hook the native loader.

The final script uses Frida 17 to hook `linker64` and `call_constructors()`, catching the moment the linker is about to run the constructors of `libdexprotector.so`:

```text
[linker call_constructors] target=.../split_config.arm64_v8a.apk!/lib/arm64-v8a/libdexprotector.so
[libdexprotector.so] base=0x7408f10000
```

Why hook here?

- It lets you observe `.init_array` before `JNI_OnLoad`.
- It captures the outer library base address at the right moment.
- It lets you hook the unpacker before the hidden image is invoked.
- It avoids hooking Java too early.

---

## 2. Outer Loader: `libdexprotector.so` Is Only Stage 0

`libdexprotector.so` is a stripped ARM64 shared object. Two important entry points are:

```text
.init_array[0] = base + 0x378
JNI_OnLoad     = base + 0x440
```

### 2.1 Constructor `sub_378`

`sub_378()` runs before the exported `JNI_OnLoad()`. It:

1. Calls syscalls/`prctl` to make dumping and debugging harder.
2. Parses its own program headers.
3. Finds the fourth `PT_LOAD` segment.
4. Calls the unpacker:

```c
sub_2434(base + 0xf630, 0x588d9)
```

5. Stores the returned function pointer in `off_B230`.
6. Stores the status in `dword_B228`.

If unpacking or linking fails, the exported `JNI_OnLoad` returns a negative error value:

```c
if (dword_B228)
    return -dword_B228; // for example, -500
```

This explains the error observed earlier:

```text
java.lang.UnsatisfiedLinkError: Bad JNI version returned ... -500
```

This is not really a Java error. It is the outer loader reporting a failure to unpack or link the hidden image.

### 2.2 Exported `JNI_OnLoad`

Decompiled logic:

```c
jint JNI_OnLoad(JavaVM *vm, void *reserved) {
    if (dword_B228)
        return -dword_B228;

    ret = off_B230(vm, 0);   // hidden entry
    off_B230 = NULL;

    if (ret)
        return -ret;

    return JNI_VERSION_1_4;  // 0x10004
}
```

Thus, the exported `JNI_OnLoad` is only a trampoline. The real code resides in the hidden image.

---

## 3. Unpacking Key: `sub_918()` and the Linker’s `r_debug->r_brk`

`sub_918()` generates a 32-byte key to decrypt the hidden payload. It does not rely solely on constants; it also mixes in the linker’s runtime state:

```text
r_debug -> r_brk -> first bytes of rtld_db_dlactivity()
```

Observed raw VM key:

```text
9b85ecd2ebec18cf24758bdd14df3e03b430508aa690a5006ef23f3c8b7d8ca2
```

Core logic:

```c
key[0]  ^= p[0];
key[4]  ^= p[1];
key[8]  ^= p[2];
key[12] ^= p[3];
```

On the test Pixel device, `r_brk` began with:

```text
e4 4c 05 14
```

But if Frida or another hook changes this region, the key changes and the payload can no longer be decrypted. The bypass script uses the idea of spoofing the bytes of an ARM64 `RET` instruction:

```text
c0 03 5f d6
```

Final forced key:

```text
5b85ecd2e8ec18cf7b758bddc2df3e03b430508aa690a5006ef23f3c8b7d8ca2
```

Script log:

```text
[outer sub_918 spoof_plan]
fixed_used4=c0035fd6
forced_final=5b85ecd2e8ec18cf7b758bddc2df3e03b430508aa690a5006ef23f3c8b7d8ca2
```

---

## 4. Unpacking Format: Decrypt, Decompress, and Link Manually

Functions renamed in IDA:

```text
sub_D4C  -> dp_cipher_ctx_zero
sub_D60  -> dp_cipher_set_key
sub_DAC  -> dp_stream_xor_crypt
sub_1290 -> dp_unpack_payload
sub_1C5C -> lz4_decompress_block
sub_167C -> custom_linker_relocate
sub_2434 -> unpack_map_link_and_run_init
```

`sub_1290()` handles the actual unpacking:

1. Calls `sub_918()` to obtain the key.
2. Initializes the stream cipher.
3. Decrypts the 36-byte header.
4. Allocates anonymous memory with `mmap`.
5. Decrypts each chunk.
6. LZ4-decompresses the chunks into the `mmap` region.
7. Checks each chunk’s checksum.
8. Saves information for a later `mprotect` call.

The 36-byte header after decryption:

```text
000009000000000040000000080000006049080000000800008000000040000003000000
```

Main fields identified:

```text
map_size_base = 0x90000
bias_delta    = 0
mapped_ptr_1  = load_bias + 0x84960
extra_off     = 0x80000
extra_size    = 0x8000
alignment     = 0x4000
chunk_count   = 3
```

An easy mistake: `mmap` only allocates a memory region. Packed data does not magically appear there. `sub_1290()` decrypts and decompresses each chunk, then writes it into the mapping.

---

## 5. Custom Linker: The Hidden Image Has No Normal ELF Header

After the chunks are placed in memory, `sub_167C()` performs the work of a dynamic linker:

```c
sub_167C(dynamic_ptr, load_bias, auxv, r_debug)
```

It parses the dynamic table:

```text
DT_STRTAB, DT_SYMTAB, DT_RELA, DT_RELASZ,
DT_JMPREL, DT_PLTRELSZ, DT_RELR, DT_RELRSZ...
```

It resolves `DT_NEEDED` dependencies by walking `r_debug->r_map`. Required libraries:

```text
libc.so
liblog.so
libandroid.so
```

SONAME of the hidden image:

```text
libdp.so
```

After relocation, it wipes the metadata:

```c
memset(symtab, 0, strtab + strsz - symtab);
```

This means that a dump taken too late will be missing relocation, symbol, and string tables. To make decompilation easier, dump at the right time or combine a post-relocation dump with metadata from a pre-relocation dump.

High-level flow of `sub_2434()`:

```text
sub_2220()       -> locate auxv using /proc/self/stat/environ layout
sub_2358(auxv)  -> locate r_debug
sub_1290(...)   -> decrypt + decompress hidden payload
sub_167C(...)   -> resolve imports + relocations
sub_15E0(...)   -> restore mprotect
call init_array -> run actual initialization
sub_1658(...)   -> reseal/reprotect
return fini_array entry -> hidden JNI entry
```

---

## 6. Hidden Entry `sub_4E354`: The Real `JNI_OnLoad`

The hidden entry point is located at:

```text
hidden+0x4E354 = sub_4E354(JavaVM *vm)
```

It begins like a real `JNI_OnLoad`:

```c
if (vm->GetEnv(vm, &env, JNI_VERSION_1_4))
    return 1201;
```

It then passes through a series of gates:

```text
sub_374A0(env)  -> cache JNI refs/classes/methods/fields
sub_367A8(env)  -> delay ContentProviders
sub_363F0()     -> SDK/system property checks
sub_5CB1C()     -> watchdog thread #1
sub_5D6F0()     -> watchdog thread #2
sub_3E038(v54)  -> mix hidden image integrity into crypto ctx
sub_16190(...)  -> protected code hash
sub_5E684(v54)  -> ic.dat integrity/decrypt
sub_4EB9C(...)  -> final env gate + Java payload bootstrap
sub_4E7D8(env, code) -> finalizer/error path
```

If `code != 0`, it creates/throws `MessageGuardException_...`. If `code == 0`, `sub_4E7D8` is not an error path; it is a success finalizer that registers the native method `ye()V`.

---

## 7. The Protector Controls ContentProviders to Preserve Initialization Order

`sub_367A8(JNIEnv *env)` directly manipulates Android framework objects.

Runtime logs show that it accesses:

```text
AppBindData.providers
ActivityThread.mInitialApplication
ActivityThread.installContentProviders(Context, List)
```

Equivalent flow:

```java
List providers = appBindData.providers;
savedProviders = NewGlobalRef(providers);
appBindData.providers = null;
activityThread.mInitialApplication = application;
cache installContentProviders(Context, List);
```

Why does it do this?

Normally, Android installs ContentProviders before `Application.onCreate()`. The protector does not want provider or app code to run before:

- The hidden image has been unpacked.
- Assets have been decrypted.
- Encrypted classes have been loaded.
- The native bridge and string decoder have been registered.
- Environment and integrity checks have finished.

It therefore saves the provider list, clears the `providers` field, and later installs those providers itself once initialization is safe.

---

## 8. `v54`: The Hidden Library’s Central Context/Key

Inside `sub_4E354`, `v54` is an important cryptographic/checking context. It is not derived solely from a static key.

Creation and mutation sequence:

```c
sub_15D88(&unk_89799, 32, &byte_8972C, 64, v54);
sub_3CF84(v54);
sub_3DDB8(v54, 1);
v30 = sub_3E038(v54);
sub_16190(...);
sub_15DD4(v54, ...);
```

Brief explanation:

| Function | Role |
|---|---|
| `sub_15D88` | Initialize the cryptographic/checking context from static configuration |
| `sub_3CF84` | Mix in APK/signing/configuration material, if present |
| `sub_3DDB8` | Mix in an additional 32-bit/static field, depending on a flag |
| `sub_3E038` | Hash the hidden image `[hidden_base, hidden_base+0x7caa0)` and mix it into the context |
| `sub_16190` | Hash protected code range `hidden+0x10e00..0x771e8` |
| `sub_15DD4` | PRF/check output; no spoofing required if `v54` is correct |

The initial mistake was hooking hidden functions too early. Since `sub_3E038()` hashes the hidden image itself, inline hooks modify its bytes and cause `v54` to be wrong. When `v54` is incorrect, decrypting `ic.dat` fails with an error such as `714`.

---

## 9. Clean Snapshot: The Least Invasive Bypass

The final solution is not to force branches arbitrarily, but to preserve a clean copy of the hidden image before installing hooks in it.

The clean moment is immediately before the outer `JNI_OnLoad` calls the hidden entry:

```text
outer libdexprotector.so+0x468  BLR hidden_entry
```

The script does this:

```js
runtimeCleanHiddenImageCopy = Memory.alloc(0x7caa0);
Memory.copy(runtimeCleanHiddenImageCopy, hidden_base, 0x7caa0);
```

Log:

```text
[runtime clean hidden snapshot]
src=0x7408e7c000 dst=0x76aac00010 len=0x7caa0
```

Afterward, for hashes/checks that use `hidden_base`, the script redirects the input to the clean snapshot at the relevant call sites.

The idea is to make the computed value equal the expected value: do not break the comparison; instead, let the check calculate the exact value the protector expects.

---

## 10. Main Bypasses

### 10.1 `sub_3E038`: Force the Clean Digest

Runtime clean digest:

```text
fce5f155a916bccade80a9c585c98f55064e0918095592ad4754aea5932a8696
```

Hook `sub_320B4` when it is called from `sub_3E038`, then write the clean digest to the output.

Log:

```text
[sub_320B4/sub_3E038 force digest #1]
source=runtime-clean ok=true
```

### 10.2 `sub_16190`: spoof hash protected range

Clean hash:

```text
sub_16190(hidden_base + 0x10E00, 0x663E8, zero_key16)
= 0xfc920bfb67d0075a
```

Use a function-level hook and spoof only the call with these arguments:

```text
data = hidden+0x10e00
len  = 0x663e8
key  = zero16
```

Log:

```text
[sub_16190 spoof protected]
real=0x894e111a2d3a72dc -> 0xfc920bfb67d0075a
```

### 10.3 Watchdog thread

Two functions spawn detached threads:

```text
sub_5CB1C -> pthread_create start=hidden+0x5CB5C
sub_5D6F0 -> pthread_create start=hidden+0x5D730
```

The script selectively blocks `pthread_create` for those exact start addresses, returning 0 as if thread creation succeeded without actually creating the threads.

Log:

```text
[watchdog BLOCK] pthread_create start=hidden+0x5cb5c ... -> 0
[watchdog BLOCK] pthread_create start=hidden+0x5d730 ... -> 0
```

### 10.4 `qword_8CEF0`: Repairing the LR Corrupted by Frida

The hidden code stores the caller’s LR in:

```text
qword_8CEF0
```

Later, `sub_5F0D0()` uses it to recover a path from `/proc/self/maps`. A Frida hook can change LR into a trampoline/anonymous address, making the path lookup fail.

Minimal fix:

```js
qword_8CEF0 = outer_libdexprotector_base + 0x46c;
saved_lr_on_stack = same_value;
```

### 10.5 `sub_5C4A4`: maps/libc/art precheck

This check is sensitive to instrumentation. The script replaces it with a function that returns 0:

```text
[precheck SKIP] sub_5C4A4_maps_libc_art_check ret -> 0
```

---

## 11. `ic.dat`: The APK Integrity Database

When `v54` is correct, `sub_5E684(v54)` successfully decrypts/decompresses `assets/ic.dat`.

Flow:

```text
sub_54944(6)       -> open "ic.dat"
sub_20754(...)     -> decrypt/auth
sub_71844(...)     -> decompress
sub_5F0D0(...)     -> verify APK/native-lib CRC path
SipHash(CRC array) -> compare expected hash
```

Log:

```text
[ic.dat decrypt leave] ret=0
[ic.dat decrypted] decomp_size=0xd2 comp_size=0x8b
[ic.dat decompress leave] written=0xd2 expected=0xd2
```

Parsed data:

```text
siphash_key16 = d4278b9ed41c9d789b9120c2348f6f3a
expected_hash = 0x2d49e542ca05617a
entry_count   = 10
```

Entries:

```text
assets/chinook.db
assets/classes.dex.dat
assets/dp.arm-v7.so.dat
assets/dp.mp3
assets/dp_db.mp3
assets/ict.dat
assets/rcdb.dat
assets/resources.dat
assets/se.dat
classes.dex
```

CRC/SipHash check passed:

```text
expected = 0x2d49e542ca05617a
computed = 0x2d49e542ca05617a
match    = true
```

---

## 12. `classes.dex.dat`: protected DEX container

`assets/classes.dex.dat` is not a single DEX file. It is a container.

Runtime flow:

```text
assets/classes.dex.dat
        ↓ sub_54944(5)
        ↓ sub_3EB6C()
        ↓ sub_3EA28()
classes_dex_dat_decrypted_container.bin
        ↓ parse offset table
classes0.dex
classes1.dex
classes2.dex
```

Successfully dumped:

```text
classes_dex_dat_00_classes0_from_container.dex  size=13186992  magic=dex\n037\0
classes_dex_dat_01_classes1_from_container.dex  size=12999919  magic=dex\n037\0
classes_dex_dat_02_classes2_from_container.dex  size=11255808  magic=dex\n037\0
```

The script does not modify checksums, patch bytecode, or rebuild anything. It extracts each DEX using the `file_size` field in its header after the protector decrypts/unpacks the container.

A small gate here uses `sub_6A05C(hidden_base, 0x7caa0, 0)` to select/load data. Under Frida, the hash is wrong. The fix is to redirect argument 0 to the clean snapshot.

Log:

```text
[classes.dex.dat selector clean-copy]
v12=0x4169c1cc97e045b1 v13=0x4169c1cc97e045b1 match=true
```

---

## 13. `dp.mp3`: Metadata for Hidden Method/Field Access

Static decoding in `sub_54944` shows that ID 4 corresponds to `dp.mp3`:

```c
case 4: blob = 0x12D1; // "dp.mp3"
case 5: blob = 0x480C; // "classes.dex.dat"
case 6: blob = 0x5F5B; // "ic.dat"
```

`sub_402E0(env, v54, flag)` opens asset ID 4:

```text
sub_54944(4, out) -> "dp.mp3"
sub_20754(...)    -> decrypt/auth
sub_71844(...)    -> decompress
```

Decompressed header:

```text
first_qword = 0x4169c1cc97e045b1
u32_20      = 8623
u32_24      = 77803
```

`first_qword` must match the clean hash of the hidden image:

```c
if (sub_6A05C(hidden_base, 0x7caa0, 0) == *(uint64_t *)decompressed)
    byte_8CB48 = 1;
```

Frida makes the hidden image dirty, so the hash fails. The fix is the same as above: when called from `sub_402E0`, redirect the data pointer from `hidden_base` to the clean snapshot.

Log:

```text
[DPMP3 hash clean-copy] caller=hidden+0x40460 arg0 hidden_base -> clean_snapshot
[DPMP3 hash leave] real=0x4169c1cc97e045b1 expected_first_qword=0x4169c1cc97e045b1 match=true
```

The table installation then proceeds normally:

```text
sub_402E0 -> sub_41108(...) -> byte_8CB48 = 1
```

In behavioral terms, `dp.mp3` is essential to hidden access/native dispatch: it maps indexes to classes, methods, fields, and the string pool.

---

## 14. `sub_55718`: The `ProtectedApplication.s` Failure Is Not Simply a Bad `RegisterNatives` Call

This is the most interesting final discovery.

`sub_55718()` sets up the native string decoder:

```text
ProtectedApplication.s(String):String
```

It calculates an HMAC/SHA256-like value:

```c
v45 = HMAC_SHA256(key = empty, data = hidden_base, len = 0x7caa0);
```

Then it compares the result with an expected 32-byte value from the `dp.mp3` payload:

```c
if (v45 == expected)
    fn = hidden + 0x56078;               // real string decoder
else
    fn = JNIEnv->CallStaticObjectMethod; // poison libart.so+0x61a24c

RegisterNatives(env, ProtectedApplication, { "s", sig, fn }, 1);
```

If we hook the hidden image beforehand, the HMAC uses bytes already modified by Frida and the comparison fails. Then `s(String)` is registered to `libart.so+0x61a24c`; when provider/class initialization later calls `ProtectedApplication.s(...)`, ART crashes.

### Earlier Bypass (Discarded)

It is possible to patch the branch or directly patch the `RegisterNatives` record so that `fn=hidden+0x56078`, but this approach is problematic:

- It can easily corrupt the stack/registers in the middle of a basic block.
- It does not preserve the original semantics.
- It may still fail if the protector also validates the native function address.

### Final Bypass

Hook `sub_15F44` at the exact call site from `sub_55718`:

```text
caller LR = hidden+0x55950
key_len   = 0
data      = hidden_base
len       = 0x7caa0
```

Change only:

```js
args[2] = runtimeCleanHiddenImageCopy;
```

Result:

```text
[sub_55718 HMAC redirect]
caller=hidden+0x55950
key_len=0
data hidden_base -> clean_snapshot
len=0x7caa0

[sub_55718 HMAC leave]
digest=7ec8a168455cf863cf5db8e8051068f0f3d0c6288e824dcfb804363c9bfa3729
expected=7ec8a168455cf863cf5db8e8051068f0f3d0c6288e824dcfb804363c9bfa3729
match=true
```

And `RegisterNatives` naturally receives the correct address:

```text
name="s"
sig="(Ljava/lang/String;)Ljava/lang/String;"
fn=hidden+0x56078
```

This is the cleanest bypass in the entire flow: no forced comparison, no patched function pointer, just the correct clean input supplied to the hash.

---

## 15. Important `RegisterNatives` Map

Observed native registrations:

```text
ProtectedApplication$ProtectedApplication.AgqpckdwFG()[B -> hidden+0x4f87c
ProtectedApplication.s(String):String                 -> hidden+0x56078
ProtectedApplication$...$QrGen$Segment.xDwEEmqjHh()   -> hidden+0x65ea0
ProtectedApplication$...$QrGen$Segment.Erq(String)    -> hidden+0x65f34
ProtectedApplication.ylGi(Object,String):InputStream  -> hidden+0x5228c
ProtectedApplication.ye()V                            -> hidden+0x4f9dc
```

`sub_4E7D8(env, 0)` is a success finalizer that registers `ye()V`; it should not be mistakenly logged as an error.

---

## 16. Final script: `bypass_dexprotector.js`

The final script keeps the number of hooks as small as possible:

```text
- linker call_constructors hook
- outer sub_918 key spoof
- clean hidden-image snapshot
- sub_3E038 clean digest fix
- sub_16190 protected hash spoof
- watchdog pthread_create block
- qword_8CEF0/saved LR restore
- sub_5C4A4 maps/libc/art precheck skip
- ic.dat decrypt/decompress dump
- dp.mp3 decrypt/decompress dump
- classes.dex.dat selector clean hash
- sub_55718 HMAC input redirect
- Minimal RegisterNatives tracing
```

Removed from the final script:

```text
- Java provider hooks installed too early
- Excessive crash/death tracing
- Excessive `sub_5F0D0` CRC-internal tracing
- Excessive post-chain return tracing
- Direct `RegisterNatives` patch for `ProtectedApplication.s`
- Branch patch in `sub_55718`
```

Run:

```bash
timeout 45s ./frida17/bin/frida -U -f com.dexprotector.detector.envchecks \
  -l bypass_dexprotector.js
```

Final evidence:

```text
[runtime clean hidden snapshot] src=0x7408e7c000 dst=0x76aac00010 len=0x7caa0
[sub_320B4/sub_3E038 force digest #1] source=runtime-clean ok=true
[sub_16190 spoof protected] real=0x894e111a2d3a72dc -> 0xfc920bfb67d0075a
[ic.dat decrypt leave] ret=0
[ic.dat decompressed] ...
[DPMP3 hash clean-copy] arg0 hidden_base -> clean_snapshot
[DPMP3 hash leave] match=true
[sub_55718 HMAC redirect] data hidden_base -> clean_snapshot
[sub_55718 HMAC leave] digest=7ec8...3729 expected=7ec8...3729 match=true
[reg 0] name="s" sig="(Ljava/lang/String;)Ljava/lang/String;" fn=... hidden+0x56078
[outer JNI_OnLoad RETSITE normal] x0=0x10004 valid=true
```

Not observed:

```text
Bad JNI version
Process crashed
FATAL EXCEPTION
```

---

## 17. Lessons Learned

### 17.1 Hooking Earlier Is Not Always Better

When a protector hashes its own code pages, hooking too early may modify the very data used to derive a key. Separate these stages:

```text
time to capture the clean snapshot
        ↓
time to install hooks
        ↓
time to redirect hash inputs to the snapshot
```

### 17.2 A Good Bypass Makes the Comparison Pass Instead of Skipping It

The most stable bypasses all follow the same pattern:

```text
the original comparison still executes
computed value == expected value
```

For example:

- `classes.dex.dat` selector hash,
- `dp.mp3` header hash,
- `sub_55718` HMAC.

### 17.3 `qword_8CEF0` Illustrates a Frida Side Effect

Even a function-level hook can change LR/the caller context enough that `/proc/self/maps` logic resolves the wrong path. Not every crash comes from a check detecting Frida; sometimes instrumentation itself corrupts the calling context.

### 17.4 Java Hooks During Hidden JNI Initialization Are Very Dangerous

Early `Java.perform()`/provider hooks previously crashed ART’s HeapTaskDaemon when the GC walked the stack. The final script intentionally remains native-only until hidden initialization is complete.

---

## Appendix A — Offset cheat sheet

### Outer `libdexprotector.so`

| Offset | Name | Notes |
|---:|---|---|
| `0x378` | `.init_array` constructor | Runs the unpacker |
| `0x440` | Exported `JNI_OnLoad` | Trampoline to hidden entry |
| `0x468` | Call hidden entry | Clean-snapshot point |
| `0x46c` | Return after hidden entry | Expected `qword_8CEF0` |
| `0x918` | `sub_918` | Derives the unpacking key |
| `0x167c` | `sub_167C` outer helper | Obtains hidden load bias |
| `0xb228` | `dword_B228` | Initialization status |
| `0xb230` | `off_B230` | Hidden entry pointer |

### Hidden image

| Offset | Name/Meaning |
|---:|---|
| `0x4e354` | Hidden JNI entry `sub_4E354` |
| `0x4e7d8` | Finalizer/error path `sub_4E7D8` |
| `0x5e684` | `ic.dat` check/decrypt/decompress |
| `0x402e0` | `dp.mp3` decrypt/decompress/init |
| `0x3eb6c` | `classes.dex.dat` loader |
| `0x55718` | Sets up `ProtectedApplication.s` |
| `0x56078` | Real `ProtectedApplication.s(String)` decoder |
| `0x65b50` | `QrGen$Segment` JNI setup |
| `0x16190` | Protected-code hash helper |
| `0x15f44` | HMAC helper used by `sub_55718` |
| `0x320b4` | SHA/digest helper used by `sub_3E038` |
| `0x6a05c` | Hash helper used by dp/classes selectors |
| `0x8cef0` | Saved LR / caller address |
| `0x8cef8` | Expected clean `sub_16190` hash |
| `0x8cb48` | `dp.mp3` table-install success flag |

---

## Appendix B — Connections to Other Write-ups

This analysis follows the same general approach as two other write-ups:

- Romain Thomas describes DexProtector as a multi-stage loader: `libdexprotector.so` decrypts/maps the hidden `libdp.so`, uses linker state to derive keys, and relies heavily on assets such as `classes.dex.dat`, `dp.mp3`, and `ic.dat`.
- The Kanxue article emphasizes a hands-on approach: rather than only hooking `System.loadLibrary`, it follows the loader/`JNI_OnLoad`, dumps anonymous executable mappings, uses LR/call sites to narrow down checks, and uses a clean copy of the text segment to pass integrity checks.

This sample shows the same pattern clearly: everything became stable once we stopped “patching branches to get past checks” and instead “let hash functions see clean text bytes.”

## References

- Romain Thomas, **A Glimpse Into DexProtector**, 2026: <https://www.romainthomas.fr/post/26-01-dexprotector/>
- Kanxue, **Bypassing Frida Detection in DexProtector-Protected Apps from Scratch** (original title: 从零开始绕过 DexProtector 加固的 Frida 检测), 2025: <https://bbs.kanxue.com/thread-289170.htm>
