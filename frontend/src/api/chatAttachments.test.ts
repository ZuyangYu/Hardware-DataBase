import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, uploadFiles } from './client';
import { uploadAttachment } from './chatAttachments';

vi.mock('./client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./client')>();
  return { ...actual, uploadFiles: vi.fn() };
});

const uploadFilesMock = vi.mocked(uploadFiles);

describe('chat attachment upload retry', () => {
  beforeEach(() => {
    uploadFilesMock.mockReset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('retries an idempotent upload after a transient gateway error', async () => {
    uploadFilesMock
      .mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', 'Bad Gateway'))
      .mockResolvedValueOnce({ attachment_id: 'att-1' } as never);

    const result = uploadAttachment(7, new File(['hello'], 'check.txt'), {
      clientRequestId: 'upload-check-1',
    });
    await vi.advanceTimersByTimeAsync(700);

    await expect(result).resolves.toMatchObject({ attachment_id: 'att-1' });
    expect(uploadFilesMock).toHaveBeenCalledTimes(2);
  });

  it('does not retry when the caller did not provide an idempotency key', async () => {
    uploadFilesMock.mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', 'Bad Gateway'));

    await expect(
      uploadAttachment(7, new File(['hello'], 'check.txt')),
    ).rejects.toMatchObject({ status: 502 });
    expect(uploadFilesMock).toHaveBeenCalledTimes(1);
  });
});
