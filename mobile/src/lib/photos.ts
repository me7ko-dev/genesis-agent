/**
 * Снимка от галерията или от камерата → смалена JPEG в base64 за `send`
 * (2026-10-15). expo-image-picker (SDK 57) дава системния избор и камерата —
 * и в браузъра, през <input type="file">; expo-image-manipulator смалява
 * (contextual API: manipulate → renderAsync → saveAsync; manipulateAsync е
 * deprecated в SDK 57).
 */
import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';
import * as ImagePicker from 'expo-image-picker';

import { base64Bytes, fitSize, imageName, JPEG_QUALITY, nextQuality, stripDataUrl } from './media';
import type { OutgoingImage } from './protocol';
import { b64urlEncode } from './protocol';
import { random } from './random';

/** Снимка над полето за писане: какво се праща и какво се показва. */
export type Attachment = OutgoingImage & { uri: string; width: number; height: number };

export type PickResult = { images: Attachment[]; error?: string };

/** Галерията: до `left` снимки наведнъж. Без разрешение за галерията — iOS и
 *  Android дават системния избор и без него (SDK 57 docs, Usage). */
export async function pickFromLibrary(left: number): Promise<PickResult> {
  if (left <= 0) return { images: [] };
  const result = await ImagePicker.launchImageLibraryAsync({
    mediaTypes: ['images'],
    allowsMultipleSelection: left > 1,
    selectionLimit: left,
    // 1 — без второ компресиране от избора: смаляваме и компресираме веднъж, тук.
    quality: 1,
    exif: false,
  });
  if (result.canceled || !result.assets?.length) return { images: [] };
  return prepareAll(result.assets.slice(0, left));
}

/** Камерата — винаги иска разрешение (SDK 57 docs). */
export async function takePhoto(): Promise<PickResult> {
  const permission = await ImagePicker.requestCameraPermissionsAsync();
  if (!permission.granted) {
    return { images: [], error: 'Няма достъп до камерата — разреши го в настройките на телефона.' };
  }
  const result = await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], quality: 1, exif: false });
  if (result.canceled || !result.assets?.length) return { images: [] };
  return prepareAll(result.assets.slice(0, 1));
}

async function prepareAll(assets: ImagePicker.ImagePickerAsset[]): Promise<PickResult> {
  const images: Attachment[] = [];
  let failed = 0;
  for (const [index, asset] of assets.entries()) {
    try {
      images.push(await prepare(asset, index));
    } catch (e) {
      console.warn('genesis: image', e instanceof Error ? e.message : String(e));
      failed += 1;
    }
  }
  return failed ? { images, error: failed === 1 ? 'Една снимка не се отвори.' : `${failed} снимки не се отвориха.` } : { images };
}

/** Смаляване до 1568 px по дългата страна и JPEG ~0.7; по-ниско качество
 *  само ако снимката остане много над целта (media.nextQuality). Размерът се
 *  взема от декодираната картинка, не от избора — там може да е 0. */
async function prepare(asset: ImagePicker.ImagePickerAsset, index: number): Promise<Attachment> {
  const original = await ImageManipulator.manipulate(asset.uri).renderAsync();
  let image = original;
  const size = fitSize(original.width, original.height);
  if (size) {
    const context = ImageManipulator.manipulate(original).resize(size);
    image = await context.renderAsync();
    context.release();
  }
  try {
    let quality = JPEG_QUALITY;
    for (;;) {
      const saved = await image.saveAsync({ base64: true, compress: quality, format: SaveFormat.JPEG });
      const data = stripDataUrl(saved.base64 ?? '');
      if (!data) throw new Error('no base64 from saveAsync');
      const next = nextQuality(base64Bytes(data), quality);
      if (next === null) {
        return { id: b64urlEncode(random(9)), mime: 'image/jpeg', data, name: imageName(asset.fileName, index),
                 uri: saved.uri, width: saved.width, height: saved.height };
      }
      quality = next;
    }
  } finally {
    if (image !== original) image.release();
    original.release();
  }
}
