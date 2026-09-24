import { describe, expect, it } from 'vitest';

import { ApiError } from './client';

describe('ApiError', () => {
  it('explains an empty proxy 500 as an unavailable API service', () => {
    const error = new ApiError(500, '', 'Internal Server Error');

    expect(error.message).toContain('无法连接后端服务');
    expect(error.message).toContain('8001');
  });

  it('keeps structured backend details for non-proxy errors', () => {
    const error = new ApiError(401, '{"detail":"invalid credentials"}', 'Unauthorized');

    expect(error.message).toBe('invalid credentials');
  });
});
