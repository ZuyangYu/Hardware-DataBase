/**
 * Copy text to the clipboard, including plain-HTTP origins (LAN IP) where the
 * async Clipboard API is unavailable and only execCommand works.
 */
export async function copyTextToClipboard(value: string): Promise<boolean> {
  if (!value) return false;

  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Permission denied or a transient failure: fall through to execCommand.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.top = '0';
  textarea.style.left = '0';
  textarea.style.opacity = '0';
  textarea.style.pointerEvents = 'none';
  document.body.appendChild(textarea);

  const selection = document.getSelection();
  const previous =
    selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, value.length);

  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch {
    copied = false;
  }

  document.body.removeChild(textarea);
  if (previous && selection) {
    selection.removeAllRanges();
    selection.addRange(previous);
  }
  return copied;
}
