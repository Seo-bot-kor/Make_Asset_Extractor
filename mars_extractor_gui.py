#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MARS Extractor (GUI)
====================
VIRNECT MAKE / ARES (.mars) AR 프로젝트 파일에서 에셋(3D 모델, 이미지, 영상, 오디오)을
추출하는 데스크톱 프로그램.

- 파일 선택 버튼 또는 창으로 드래그&드롭
- 종류별(models/images/videos/audio) 자동 분류
- 진행률 표시
- 추출 완료 후 폴더 자동 열기

빌드(Windows .exe):  build_windows.bat 참고
"""

# 이 숫자가 자동 업데이트의 기준입니다. 코드를 고칠 때마다 1씩 올리세요.
APP_VERSION = 4

import os
import sys
import struct
import gzip
import json
import re
import threading
import subprocess
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 선택적: tkinterdnd2 가 있으면 드래그&드롭 지원
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except Exception:
    _HAS_DND = False


# ----------------------------------------------------------------------
# FBX 바이너리에서 임베드 텍스처 추출
# ----------------------------------------------------------------------

def parse_fbx_videos(data):
    """FBX 바이너리에서 임베드된 (파일명, 이미지바이트) 목록 반환."""
    if data[:20] != b'Kaydara FBX Binary  ':
        return []
    version = struct.unpack_from('<I', data, 23)[0]
    is64 = version >= 7500
    HDR = 27

    def read_prop(p):
        t = chr(data[p]); p += 1
        if t == 'C': return p + 1, bool(data[p])
        if t == 'B': return p + 1, data[p]
        if t == 'Y': return p + 2, struct.unpack_from('<h', data, p)[0]
        if t == 'I': return p + 4, struct.unpack_from('<i', data, p)[0]
        if t == 'F': return p + 4, struct.unpack_from('<f', data, p)[0]
        if t == 'D': return p + 8, struct.unpack_from('<d', data, p)[0]
        if t == 'L': return p + 8, struct.unpack_from('<q', data, p)[0]
        if t in ('S', 'R'):
            ln = struct.unpack_from('<I', data, p)[0]; p += 4
            return p + ln, data[p:p + ln]
        if t in 'fdlibc':
            arr_len, enc, comp_len = struct.unpack_from('<III', data, p); p += 12
            return p + comp_len, ('array', enc, data[p:p + comp_len])
        raise ValueError(f"unknown prop type {t!r}")

    def read_node(pos):
        if is64:
            end_off, num_props, prop_len = struct.unpack_from('<QQQ', data, pos); hp = pos + 24
            null_sz = 25
        else:
            end_off, num_props, prop_len = struct.unpack_from('<III', data, pos); hp = pos + 12
            null_sz = 13
        name_len = data[hp]; hp += 1
        name = data[hp:hp + name_len].decode('latin-1'); hp += name_len
        if end_off == 0:
            return None, 0
        p = hp
        props = []
        for _ in range(num_props):
            p, v = read_prop(p)
            props.append(v)
        children = []
        cpos = hp + prop_len
        while cpos < end_off - null_sz:
            child, nxt = read_node(cpos)
            if child is None:
                break
            children.append(child)
            cpos = nxt
        return {'name': name, 'props': props, 'children': children}, end_off

    roots = []
    pos = HDR
    try:
        while pos < len(data) - (25 if is64 else 13):
            node, end = read_node(pos)
            if node is None or end == 0:
                break
            roots.append(node); pos = end
    except Exception:
        pass

    videos = []

    def walk(n):
        if n['name'] == 'Video':
            fname = None; content = None
            for c in n['children']:
                if c['name'] == 'RelativeFilename' and c['props']:
                    fname = c['props'][0]
                elif c['name'] == 'Filename' and c['props'] and fname is None:
                    fname = c['props'][0]
                elif c['name'] == 'Content' and c['props']:
                    pv = c['props'][0]
                    if isinstance(pv, bytes) and len(pv) > 0:
                        content = pv
            if content:
                videos.append((fname, content))
        for c in n['children']:
            walk(c)
    for r in roots:
        walk(r)
    return videos


# ----------------------------------------------------------------------
# 추출 핵심 로직
# ----------------------------------------------------------------------

SUBDIRS = {
    '.fbx': 'models', '.obj': 'models', '.glb': 'models', '.gltf': 'models',
    '.png': 'images', '.jpg': 'images', '.jpeg': 'images', '.gif': 'images',
    '.mp4': 'videos', '.mov': 'videos',
    '.wav': 'audio', '.mp3': 'audio', '.ogg': 'audio',
}

META_TARGETS = {
    'ResourceListInfo': 'ResourceListInfo.json',
    'ContentsInfo': 'ContentsInfo.json',
    'TTSData': 'TTSData.json',
    'VersionInfo': 'VersionInfo.json',
    'EnvironmentInfo': 'EnvironmentInfo.json',
    'MARS_ONLY_DATA': 'MARS_ONLY_DATA.json',
}


def sniff_ext(payload):
    if payload[:7] == b'Kaydara':
        return '.fbx'
    if payload[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if payload[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if payload[:4] == b'RIFF' and payload[8:12] == b'WAVE':
        return '.wav'
    if payload[:3] == b'ID3' or payload[:2] == b'\xff\xfb':
        return '.mp3'
    if payload[4:8] == b'ftyp':
        return '.mp4'
    if payload[:4] == b'GIF8':
        return '.gif'
    if payload[:4] == b'OggS':
        return '.ogg'
    return None


def safe_name(path_str):
    base = re.split(r'[\\/]', path_str)[-1].strip()
    base = re.sub(r'[<>:"|?*\x00-\x1f]', '_', base)
    return base or 'unnamed'


def read_top_chunks(data):
    chunks = {}
    i, n = 0, len(data)
    while i < n:
        if i + 4 > n:
            break
        klen = struct.unpack_from('<I', data, i)[0]
        if klen <= 0 or klen > 1000:
            break
        i += 4
        try:
            key = data[i:i + klen * 2].decode('utf-16-le')
        except Exception:
            break
        i += klen * 2
        if i + 4 > n:
            break
        plen = struct.unpack_from('<I', data, i)[0]
        i += 4
        chunks[key] = (i, plen)
        i += plen
    return chunks


def parse_resources(buf):
    out = []
    i, n = 0, len(buf)
    while i < n:
        if i + 4 > n:
            break
        klen = struct.unpack_from('<I', buf, i)[0]
        if klen <= 0 or klen > 4000:
            break
        i += 4
        try:
            key = buf[i:i + klen * 2].decode('utf-16-le')
        except Exception:
            break
        i += klen * 2
        if i + 4 > n:
            break
        plen = struct.unpack_from('<I', buf, i)[0]
        i += 4
        out.append((key, buf[i:i + plen]))
        i += plen
    return out


def extract_mars(mars_path, out_dir, progress_cb=None, log_cb=None, extract_textures=True):
    """
    .mars 추출. progress_cb(done, total), log_cb(str) 콜백 지원.
    extract_textures=True 이면 FBX 안의 임베드 텍스처도 이미지로 추출.
    반환: (추출개수, 총바이트, 요약dict)
    """
    def log(m):
        if log_cb:
            log_cb(m)

    log(f"파일 읽는 중: {os.path.basename(mars_path)}")
    with open(mars_path, 'rb') as f:
        head = f.read(2)
        f.seek(0)
        raw = f.read()

    data = gzip.decompress(raw) if head == b'\x1f\x8b' else raw

    chunks = read_top_chunks(data)
    if 'Resources' not in chunks:
        raise ValueError("지원되지 않는 파일 형식입니다 (Resources 청크 없음).")

    off, ln = chunks['Resources']
    res_raw = data[off:off + ln]
    res_buf = gzip.decompress(res_raw) if res_raw[:2] == b'\x1f\x8b' else res_raw

    entries = parse_resources(res_buf)
    total = len(entries)
    log(f"에셋 {total}개 발견. 추출 시작...")

    os.makedirs(out_dir, exist_ok=True)
    used = set()
    count = 0
    total_bytes = 0
    summary = {}
    fbx_items = []  # (저장된 파일명, payload)

    for idx, (key, payload) in enumerate(entries, 1):
        name = safe_name(key)
        ext = os.path.splitext(name)[1].lower() or (sniff_ext(payload) or '.bin')
        if os.path.splitext(name)[1] == '':
            name += ext

        sub = SUBDIRS.get(ext, 'other')
        target_dir = os.path.join(out_dir, sub)
        os.makedirs(target_dir, exist_ok=True)

        final = name
        stem, e = os.path.splitext(name)
        k = 1
        while final.lower() in used:
            final = f"{stem}_{k}{e}"
            k += 1
        used.add(final.lower())

        with open(os.path.join(target_dir, final), 'wb') as wf:
            wf.write(payload)

        count += 1
        total_bytes += len(payload)
        summary[ext] = summary.get(ext, 0) + 1
        if ext == '.fbx':
            fbx_items.append((final, payload))
        if progress_cb:
            progress_cb(idx, total)

    # --- FBX 임베드 텍스처 추출 ---
    if extract_textures and fbx_items:
        log("모델 텍스처 추출 중...")
        tex_count = 0
        for model_name, payload in fbx_items:
            try:
                vids = parse_fbx_videos(payload)
            except Exception:
                vids = []
            if not vids:
                continue
            model_stem = os.path.splitext(model_name)[0]
            tex_dir = os.path.join(out_dir, 'textures', model_stem)
            os.makedirs(tex_dir, exist_ok=True)
            tused = set()
            for fn, ct in vids:
                if isinstance(fn, bytes):
                    tname = fn.decode('latin-1', 'replace')
                else:
                    tname = str(fn) if fn else 'texture'
                tname = re.split(r'[\\/]', tname)[-1].strip()
                tname = re.sub(r'[<>:"|?*\x00-\x1f]', '_', tname) or 'texture'
                if '.' not in tname:
                    tname += (sniff_ext(ct) or '.png')
                tf = tname
                ts, te = os.path.splitext(tname)
                j = 1
                while tf.lower() in tused:
                    tf = f"{ts}_{j}{te}"
                    j += 1
                tused.add(tf.lower())
                with open(os.path.join(tex_dir, tf), 'wb') as wf:
                    wf.write(ct)
                tex_count += 1
        if tex_count:
            summary['(텍스처)'] = tex_count
            log(f"텍스처 {tex_count}개 추출 완료")

    # 메타데이터
    meta_dir = os.path.join(out_dir, '_metadata')
    os.makedirs(meta_dir, exist_ok=True)
    for ck, fname in META_TARGETS.items():
        if ck in chunks:
            o, l = chunks[ck]
            blob = data[o:o + l]
            if blob[:2] == b'\x1f\x8b':
                try:
                    blob = gzip.decompress(blob)
                except Exception:
                    pass
            with open(os.path.join(meta_dir, fname), 'wb') as wf:
                wf.write(blob)

    log(f"완료: {count}개 / {total_bytes/1024/1024:.1f} MB")
    return count, total_bytes, summary


# ----------------------------------------------------------------------
# glTF/GLB (.make / .glb / .gltf) 추출
# ----------------------------------------------------------------------

def _write_glb(gltf_json, bin_data, out_path):
    """glTF dict + binary 를 .glb 파일로 기록."""
    js = json.dumps(gltf_json, ensure_ascii=False).encode('utf-8')
    while len(js) % 4 != 0:
        js += b' '
    bn = bytearray(bin_data)
    while len(bn) % 4 != 0:
        bn.append(0)
    total = 12 + 8 + len(js) + 8 + len(bn)
    with open(out_path, 'wb') as f:
        f.write(b'glTF')
        f.write(struct.pack('<II', 2, total))
        f.write(struct.pack('<I', len(js)))
        f.write(b'JSON')
        f.write(js)
        f.write(struct.pack('<I', len(bn)))
        f.write(b'BIN\x00')
        f.write(bytes(bn))


def _build_single_mesh_glb(gltf, bin_data, mesh_idx, node=None):
    """원본 glTF 에서 mesh_idx 하나만 떼어 독립 glTF dict + binary 생성."""
    import copy
    # 원본 generator 정보는 유지하되, glTF 버전은 표준 "2.0"으로 강제.
    # (VIRNECT .make 는 version 칸에 자체 콘텐츠 버전 "1.0.2-pre.6" 를
    #  넣어두는데, Blender 등 표준 뷰어가 이를 거부하므로 바로잡는다.)
    src_asset = gltf.get('asset', {})
    g = {'asset': {'version': '2.0'}}
    if src_asset.get('generator'):
        g['asset']['generator'] = src_asset['generator']
    new_bv = []; new_acc = []; new_mat = []; new_tex = []; new_img = []; new_samp = []
    blob = bytearray()
    bv_map = {}; acc_map = {}; mat_map = {}; tex_map = {}; img_map = {}; samp_map = {}

    def add_bufferview(bvi):
        if bvi in bv_map:
            return bv_map[bvi]
        bv = gltf['bufferViews'][bvi]
        off = bv.get('byteOffset', 0); ln = bv['byteLength']
        while len(blob) % 4 != 0:
            blob.append(0)
        new_off = len(blob)
        blob.extend(bin_data[off:off + ln])
        nbv = {'buffer': 0, 'byteOffset': new_off, 'byteLength': ln}
        if 'byteStride' in bv:
            nbv['byteStride'] = bv['byteStride']
        if 'target' in bv:
            nbv['target'] = bv['target']
        new_bv.append(nbv); bv_map[bvi] = len(new_bv) - 1
        return bv_map[bvi]

    def add_accessor(ai):
        if ai in acc_map:
            return acc_map[ai]
        a = copy.deepcopy(gltf['accessors'][ai])
        if a.get('bufferView') is not None:
            a['bufferView'] = add_bufferview(a['bufferView'])
        new_acc.append(a); acc_map[ai] = len(new_acc) - 1
        return acc_map[ai]

    def add_image(ii):
        if ii in img_map:
            return img_map[ii]
        im = copy.deepcopy(gltf['images'][ii])
        if im.get('bufferView') is not None:
            im['bufferView'] = add_bufferview(im['bufferView'])
        new_img.append(im); img_map[ii] = len(new_img) - 1
        return img_map[ii]

    def add_sampler(si):
        if si in samp_map:
            return samp_map[si]
        new_samp.append(copy.deepcopy(gltf['samplers'][si]))
        samp_map[si] = len(new_samp) - 1
        return samp_map[si]

    def add_texture(ti):
        if ti in tex_map:
            return tex_map[ti]
        t = copy.deepcopy(gltf['textures'][ti])
        if t.get('source') is not None:
            t['source'] = add_image(t['source'])
        if t.get('sampler') is not None:
            t['sampler'] = add_sampler(t['sampler'])
        new_tex.append(t); tex_map[ti] = len(new_tex) - 1
        return tex_map[ti]

    def fix_texref(d, key):
        if key in d and isinstance(d[key], dict) and 'index' in d[key]:
            d[key]['index'] = add_texture(d[key]['index'])

    def add_material(mi):
        if mi is None:
            return None
        if mi in mat_map:
            return mat_map[mi]
        m = copy.deepcopy(gltf['materials'][mi])
        pbr = m.get('pbrMetallicRoughness', {})
        fix_texref(pbr, 'baseColorTexture')
        fix_texref(pbr, 'metallicRoughnessTexture')
        fix_texref(m, 'normalTexture')
        fix_texref(m, 'occlusionTexture')
        fix_texref(m, 'emissiveTexture')
        new_mat.append(m); mat_map[mi] = len(new_mat) - 1
        return mat_map[mi]

    src_mesh = gltf['meshes'][mesh_idx]
    nm = copy.deepcopy(src_mesh)
    for prim in nm['primitives']:
        prim['attributes'] = {k: add_accessor(v) for k, v in prim['attributes'].items()}
        if 'indices' in prim:
            prim['indices'] = add_accessor(prim['indices'])
        if prim.get('material') is not None:
            prim['material'] = add_material(prim['material'])

    g['meshes'] = [nm]
    g['accessors'] = new_acc
    g['bufferViews'] = new_bv
    if new_mat:
        g['materials'] = new_mat
    if new_tex:
        g['textures'] = new_tex
    if new_img:
        g['images'] = new_img
    if new_samp:
        g['samplers'] = new_samp
    g['buffers'] = [{'byteLength': len(blob)}]
    nd = {'mesh': 0, 'name': src_mesh.get('name', 'mesh')}
    if node:
        for k in ('translation', 'rotation', 'scale', 'matrix'):
            if k in node:
                nd[k] = node[k]
    g['nodes'] = [nd]
    g['scenes'] = [{'nodes': [0]}]
    g['scene'] = 0
    return g, bytes(blob)


def extract_glb(path, out_dir, progress_cb=None, log_cb=None, extract_textures=True):
    """
    VIRNECT XR (.make) = 표준 glTF Binary 컨테이너.
    모델을 오브젝트(메시)별 .glb 로 분리하고, 텍스처를 이미지로 추출.
    """
    def log(m):
        if log_cb:
            log_cb(m)

    log(f"glTF 파일 읽는 중: {os.path.basename(path)}")
    data = open(path, 'rb').read()
    if data[:4] != b'glTF':
        raise ValueError("glTF Binary 형식이 아닙니다.")

    pos = 12
    chunks = []
    while pos < len(data):
        clen, ctype = struct.unpack_from('<II', data, pos)
        chunks.append((struct.pack('<I', ctype), pos + 8, clen))
        pos += 8 + clen

    gltf = json.loads(data[chunks[0][1]:chunks[0][1] + chunks[0][2]])
    # BIN 청크 시작 오프셋
    bin_off = None
    for ctype, o, l in chunks:
        if ctype[:3] == b'BIN':
            bin_off = o
            break
    bv = gltf.get('bufferViews', [])

    # 텍스처 → 슬롯 이름 매핑 (baseColor / metallicRoughness / normal / ...)
    slot_of = {}

    def note(ti, slot):
        if ti is None:
            return
        src = gltf['textures'][ti].get('source')
        if src is not None:
            slot_of.setdefault(src, set()).add(slot)

    for m in gltf.get('materials', []):
        pbr = m.get('pbrMetallicRoughness', {})
        if 'baseColorTexture' in pbr:
            note(pbr['baseColorTexture']['index'], 'baseColor')
        if 'metallicRoughnessTexture' in pbr:
            note(pbr['metallicRoughnessTexture']['index'], 'metallicRoughness')
        if 'normalTexture' in m:
            note(m['normalTexture']['index'], 'normal')
        if 'occlusionTexture' in m:
            note(m['occlusionTexture']['index'], 'occlusion')
        if 'emissiveTexture' in m:
            note(m['emissiveTexture']['index'], 'emissive')

    images = gltf.get('images', [])
    total = len(images)

    # --- 모델을 오브젝트(메시)별 .glb 파일로 분리 저장 ---
    bin_data = data[bin_off:]
    meshes = gltf.get('meshes', [])
    # 각 메시를 처음 참조하는 노드(위치/회전 정보)를 매핑
    node_of_mesh = {}
    for nd in gltf.get('nodes', []):
        mi = nd.get('mesh')
        if mi is not None and mi not in node_of_mesh:
            node_of_mesh[mi] = nd

    models_dir = os.path.join(out_dir, 'models')
    os.makedirs(models_dir, exist_ok=True)
    planes_dir = os.path.join(out_dir, 'image_planes')
    mused = set()
    pused = set()
    model_count = 0
    plane_count = 0
    acc = gltf.get('accessors', [])

    def is_flat_plane(mesh):
        """정점 4개·삼각형 2개짜리 단일 면 = UI/이미지/영상용 평면."""
        prims = mesh.get('primitives', [])
        if len(prims) != 1:
            return False
        a = prims[0].get('attributes', {})
        if 'POSITION' not in a:
            return False
        vtot = acc[a['POSITION']]['count']
        itot = acc[prims[0]['indices']]['count'] if 'indices' in prims[0] else 0
        return vtot == 4 and itot // 3 == 2

    log(f"모델 오브젝트 {len(meshes)}개 분리 중 (3D 형상 / 이미지 평면 구분)...")
    for mi, mesh in enumerate(meshes):
        try:
            g, bd = _build_single_mesh_glb(gltf, bin_data, mi, node_of_mesh.get(mi))
        except Exception as e:
            log(f"  [건너뜀] 메시 {mi}: {e}")
            continue
        nm = mesh.get('name') or f'mesh_{mi}'
        nm = re.sub(r'[<>:"|?*\\/\x00-\x1f]', '_', str(nm)).strip() or f'mesh_{mi}'

        if is_flat_plane(mesh):
            os.makedirs(planes_dir, exist_ok=True)
            fn = f"{mi:03d}_{nm}.glb"
            while fn.lower() in pused:
                fn = '_' + fn
            pused.add(fn.lower())
            _write_glb(g, bd, os.path.join(planes_dir, fn))
            plane_count += 1
        else:
            fn = f"{mi:03d}_{nm}.glb"
            while fn.lower() in mused:
                fn = '_' + fn
            mused.add(fn.lower())
            _write_glb(g, bd, os.path.join(models_dir, fn))
            model_count += 1
    log(f"3D 모델 {model_count}개 → models/, 이미지 평면 {plane_count}개 → image_planes/")

    log(f"이미지(텍스처) {total}개 발견. 추출 중...")

    img_dir = os.path.join(out_dir, 'images')
    os.makedirs(img_dir, exist_ok=True)
    used = set()
    count = 0
    total_bytes = 0
    summary = {'.glb(3D모델)': model_count}
    if plane_count:
        summary['.glb(이미지평면)'] = plane_count

    EXT = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}

    for i, img in enumerate(images, 1):
        idx = i - 1
        if img.get('bufferView') is None:
            continue
        view = bv[img['bufferView']]
        start = bin_off + view.get('byteOffset', 0)
        ln = view['byteLength']
        blob = data[start:start + ln]
        mime = img.get('mimeType', '')
        ext = EXT.get(mime) or (sniff_ext(blob) or '.bin')

        slots = slot_of.get(idx)
        slot_tag = ('_' + '_'.join(sorted(slots))) if slots else ''
        raw = img.get('name') or f'image_{idx}'
        raw = re.sub(r'[<>:"|?*\\/\x00-\x1f]', '_', str(raw)).strip() or f'image_{idx}'
        fn = f"{idx:03d}_{raw}{slot_tag}{ext}"
        while fn.lower() in used:
            fn = '_' + fn
        used.add(fn.lower())

        with open(os.path.join(img_dir, fn), 'wb') as wf:
            wf.write(blob)
        count += 1
        total_bytes += len(blob)
        summary[ext] = summary.get(ext, 0) + 1
        if progress_cb:
            progress_cb(i, total)

    # 메타데이터: glTF JSON 통째로 저장 (씬 구조/노드/머티리얼 분석용)
    meta_dir = os.path.join(out_dir, '_metadata')
    os.makedirs(meta_dir, exist_ok=True)
    with open(os.path.join(meta_dir, 'scene.gltf.json'), 'w', encoding='utf-8') as wf:
        json.dump(gltf, wf, ensure_ascii=False, indent=1)

    # 슬롯 분포 요약 로그
    from collections import Counter
    sc = Counter()
    for s in slot_of.values():
        for x in s:
            sc[x] += 1
    if sc:
        log("텍스처 슬롯: " + ", ".join(f"{k} {v}" for k, v in sc.items()))

    log(f"완료: {count}개 / {total_bytes/1024/1024:.1f} MB")
    return count, total_bytes, summary


def extract_any(path, out_dir, progress_cb=None, log_cb=None, extract_textures=True):
    """확장자/매직으로 .mars 와 .make(glTF) 자동 판별 후 추출."""
    with open(path, 'rb') as f:
        head = f.read(4)
    if head == b'glTF':
        return extract_glb(path, out_dir, progress_cb, log_cb, extract_textures)
    return extract_mars(path, out_dir, progress_cb, log_cb, extract_textures)


def open_folder(path):
    try:
        if sys.platform.startswith('win'):
            os.startfile(path)  # noqa
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.mars_path = None
        root.title("MARS Extractor — .mars 에셋 추출기")
        root.geometry("560x440")
        root.minsize(520, 400)

        pad = dict(padx=14, pady=6)

        title = tk.Label(root, text="MARS / ARES (.mars) 에셋 추출기",
                         font=("Malgun Gothic", 14, "bold"))
        title.pack(pady=(16, 2))
        sub = tk.Label(root, text="3D 모델 · 이미지 · 영상 · 오디오를 한 번에 추출합니다",
                       font=("Malgun Gothic", 9), fg="#666")
        sub.pack()

        # 드롭 영역
        self.drop = tk.Label(
            root,
            text=("여기로 .mars / .make 파일을 끌어다 놓으세요\n\n또는 아래 [파일 선택] 버튼을 누르세요"
                  if _HAS_DND else
                  "아래 [파일 선택] 버튼을 눌러 .mars / .make 파일을 선택하세요"),
            relief="ridge", bd=2, height=6, fg="#444",
            font=("Malgun Gothic", 10), bg="#f5f5f7")
        self.drop.pack(fill="x", **pad)

        if _HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind('<<Drop>>', self.on_drop)

        # 파일 경로 표시
        self.path_var = tk.StringVar(value="선택된 파일 없음")
        tk.Label(root, textvariable=self.path_var, font=("Malgun Gothic", 9),
                 fg="#1a5", wraplength=520, justify="left").pack(**pad)

        # 버튼들
        btns = tk.Frame(root)
        btns.pack(pady=4)
        self.btn_select = tk.Button(btns, text="파일 선택", width=14,
                                    command=self.select_file)
        self.btn_select.grid(row=0, column=0, padx=6)
        self.btn_run = tk.Button(btns, text="추출 시작", width=14,
                                 state="disabled", command=self.run)
        self.btn_run.grid(row=0, column=1, padx=6)

        # 옵션: 모델 텍스처 추출
        self.tex_var = tk.BooleanVar(value=True)
        tk.Checkbutton(root, text="모델(.fbx) 안의 텍스처도 이미지로 추출",
                       variable=self.tex_var,
                       font=("Malgun Gothic", 9)).pack(pady=(2, 0))

        # 진행률
        self.progress = ttk.Progressbar(root, length=520, mode="determinate")
        self.progress.pack(**pad)

        # 로그
        self.log_text = tk.Text(root, height=8, font=("Consolas", 9),
                                bg="#1e1e1e", fg="#d4d4d4", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=14, pady=(4, 14))

    # --- 이벤트 ---
    def on_drop(self, event):
        path = event.data.strip().strip('{}')
        if path.lower().endswith(('.mars', '.make', '.glb', '.gltf')):
            self.set_file(path)
        else:
            messagebox.showwarning("형식 오류", ".mars 또는 .make 파일을 넣어주세요.")

    def select_file(self):
        path = filedialog.askopenfilename(
            title="파일 선택 (.mars / .make)",
            filetypes=[("AR/XR 프로젝트", "*.mars *.make *.glb *.gltf"),
                       ("모든 파일", "*.*")])
        if path:
            self.set_file(path)

    def set_file(self, path):
        self.mars_path = path
        self.path_var.set(path)
        self.btn_run.config(state="normal")
        self.log(f"선택됨: {os.path.basename(path)}")

    def log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def set_progress(self, done, total):
        self.progress['maximum'] = total
        self.progress['value'] = done
        self.root.update_idletasks()

    def run(self):
        if not self.mars_path:
            return
        out_dir = os.path.join(
            os.path.dirname(self.mars_path),
            os.path.splitext(os.path.basename(self.mars_path))[0] + "_extracted")
        self.btn_run.config(state="disabled")
        self.btn_select.config(state="disabled")
        self.progress['value'] = 0

        def worker():
            try:
                count, total_bytes, summary = extract_any(
                    self.mars_path, out_dir,
                    progress_cb=lambda d, t: self.root.after(0, self.set_progress, d, t),
                    log_cb=lambda m: self.root.after(0, self.log, m),
                    extract_textures=self.tex_var.get())
                detail = ", ".join(f"{k} {v}개" for k, v in
                                   sorted(summary.items(), key=lambda x: -x[1]))
                self.root.after(0, self.log, f"구성: {detail}")
                self.root.after(0, lambda: messagebox.showinfo(
                    "추출 완료",
                    f"{count}개 에셋 추출 완료 ({total_bytes/1024/1024:.1f} MB)\n\n{detail}\n\n폴더를 엽니다."))
                self.root.after(0, open_folder, out_dir)
            except Exception as e:
                tb = traceback.format_exc()
                self.root.after(0, self.log, "오류: " + str(e))
                self.root.after(0, lambda: messagebox.showerror("오류", str(e) + "\n\n" + tb))
            finally:
                self.root.after(0, lambda: self.btn_run.config(state="normal"))
                self.root.after(0, lambda: self.btn_select.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


def main():
    if _HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
