import { expect, type Locator } from '@playwright/test';

/** Verify the actual rendered focus indicator and its bounds inside scroll containers. */
export async function expectVisibleFormFocus(control: Locator, mode: 'mouse' | 'keyboard', indicator = control) {
  await control.evaluate(element => (element as HTMLElement).blur());
  const before = await indicator.evaluate(element => {
    const style = getComputedStyle(element);
    return { borders: [style.borderTopColor, style.borderRightColor, style.borderBottomColor, style.borderLeftColor],
      shadow: style.boxShadow };
  });
  if (mode === 'mouse') await control.click();
  else {
    await control.page().keyboard.press('Tab');
    await control.focus();
  }
  await expect(control).toBeFocused();
  await expect.poll(() => indicator.evaluate((element, previous) => {
    const style = getComputedStyle(element);
    const borders = [style.borderTopColor, style.borderRightColor, style.borderBottomColor, style.borderLeftColor];
    return (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) >= 2)
      || (style.boxShadow !== 'none' && style.boxShadow !== previous.shadow)
      || borders.every((color, index) => color !== previous.borders[index]);
  }, before), { message: 'Focused form control must have a visible indicator' }).toBe(true);
  await control.scrollIntoViewIfNeeded();
  const bounds = await indicator.evaluate((element, previousShadow) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    let extent = style.outlineStyle === 'none' ? 0 : Math.max(0, parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset));
    for (const shadow of style.boxShadow.split(/,(?![^(]*\))/)) {
      if (previousShadow.split(/,(?![^(]*\))/).map(value => value.trim()).includes(shadow.trim())) continue;
      if (shadow.includes('inset') || /rgba\([^)]*,\s*0\)/.test(shadow)) continue;
      const pixels = [...shadow.matchAll(/(-?\d+(?:\.\d+)?)px/g)].map(match => Number(match[1]));
      if (pixels.length >= 4) extent = Math.max(extent, Math.abs(pixels[0]), Math.abs(pixels[1]), pixels[2] + pixels[3]);
    }
    const clip = { left: 0, right: innerWidth, top: 0, bottom: innerHeight };
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      const css = getComputedStyle(parent);
      const box = parent.getBoundingClientRect();
      if (css.overflowX !== 'visible') {
        clip.left = Math.max(clip.left, box.left + parent.clientLeft);
        clip.right = Math.min(clip.right, box.left + parent.clientLeft + parent.clientWidth);
      }
      if (css.overflowY !== 'visible') {
        clip.top = Math.max(clip.top, box.top + parent.clientTop);
        clip.bottom = Math.min(clip.bottom, box.top + parent.clientTop + parent.clientHeight);
      }
    }
    return { left: rect.left - extent - clip.left, right: clip.right - rect.right - extent,
      top: rect.top - extent - clip.top, bottom: clip.bottom - rect.bottom - extent };
  }, before.shadow);
  for (const [edge, clearance] of Object.entries(bounds)) {
    expect(clearance, `${edge} focus indicator must not be clipped`).toBeGreaterThanOrEqual(-1);
  }
}
