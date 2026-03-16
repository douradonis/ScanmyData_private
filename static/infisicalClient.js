import fetch from 'node-fetch';
import { env } from '../../config/env.js';
import { infisicalRateLimiter } from './rateLimiter.js';

const CACHE_TTL_MS = 60 * 1000;
const RETRIES = 3;
const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504]);

let fetchImpl = fetch;
const cache = new Map();

function baseUrl() {
  return env.infisicalBaseUrl || 'https://app.infisical.com';
}

function buildUrl(pathname, query = {}) {
  const url = new URL(pathname, baseUrl());
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value));
    }
  });
  return url.toString();
}

function assertConfigured() {
  if (!env.infisicalToken || !env.infisicalProjectId || !env.infisicalEnvironment) {
    throw new Error('Infisical configuration missing');
  }
}

function getCache(key) {
  const record = cache.get(key);
  if (!record) return null;
  if (Date.now() > record.expiresAt) {
    cache.delete(key);
    return null;
  }
  return record.value;
}

function setCache(key, value, ttl = CACHE_TTL_MS) {
  cache.set(key, { value, expiresAt: Date.now() + ttl });
}

function clearCache() {
  cache.clear();
}

function parseSecretResponse(data) {
  if (!data) return null;
  if (typeof data.secretValue === 'string') return data.secretValue;
  if (typeof data.secret === 'string') return data.secret;
  if (data.secret?.secretValue) return data.secret.secretValue;
  if (data.secret?.secret) return data.secret.secret;
  return null;
}

function parseSecretsMap(data) {
  const secrets = data?.secrets || [];
  const output = {};
  for (const item of secrets) {
    const key = item.secretKey || item.key;
    const value = item.secretValue ?? item.secret;
    if (key) output[key] = value;
  }
  return output;
}

async function request(pathname, { method = 'GET', body, query } = {}) {
  assertConfigured();

  let attempt = 0;
  while (attempt < RETRIES) {
    attempt += 1;
    await infisicalRateLimiter.take(1);
    const response = await fetchImpl(buildUrl(pathname, query), {
      method,
      headers: {
        Authorization: `Bearer ${env.infisicalToken}`,
        'Content-Type': 'application/json'
      },
      body: body ? JSON.stringify(body) : undefined
    });

    if (response.ok) {
      if (response.status === 204) return null;
      return response.json();
    }

    if (!RETRYABLE_STATUS.has(response.status) || attempt >= RETRIES) {
      throw new Error(`Infisical request failed: ${response.status}`);
    }

    await new Promise((resolve) => setTimeout(resolve, attempt * 200));
  }

  throw new Error('Infisical request failed unexpectedly');
}

export async function getAllSecrets({ forceRefresh = false } = {}) {
  const cacheKey = 'all-secrets';
  const cached = forceRefresh ? null : getCache(cacheKey);
  if (cached) return cached;

  const data = await request('/api/v3/secrets/raw', {
    method: 'GET',
    query: {
      workspaceId: env.infisicalProjectId,
      environment: env.infisicalEnvironment,
      secretPath: '/'
    }
  });

  const parsed = parseSecretsMap(data);
  setCache(cacheKey, parsed);
  return parsed;
}

export async function getSecret(key, { forceRefresh = false } = {}) {
  const cacheKey = `secret:${key}`;
  const cached = forceRefresh ? null : getCache(cacheKey);
  if (cached !== null) return cached;

  const data = await request(`/api/v3/secrets/raw/${encodeURIComponent(key)}`, {
    method: 'GET',
    query: {
      workspaceId: env.infisicalProjectId,
      environment: env.infisicalEnvironment,
      secretPath: '/'
    }
  });

  const value = parseSecretResponse(data);
  if (value !== null) setCache(cacheKey, value);
  return value;
}

export async function createSecret(key, value) {
  await request(`/api/v3/secrets/raw/${encodeURIComponent(key)}`, {
    method: 'POST',
    body: {
      workspaceId: env.infisicalProjectId,
      environment: env.infisicalEnvironment,
      secretPath: '/',
      secretValue: value
    }
  });
  clearCache();
}

export async function updateSecret(key, value) {
  await request(`/api/v3/secrets/raw/${encodeURIComponent(key)}`, {
    method: 'PATCH',
    body: {
      workspaceId: env.infisicalProjectId,
      environment: env.infisicalEnvironment,
      secretPath: '/',
      secretValue: value
    }
  });
  clearCache();
}

export async function deleteSecret(key) {
  await request(`/api/v3/secrets/raw/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    body: {
      workspaceId: env.infisicalProjectId,
      environment: env.infisicalEnvironment,
      secretPath: '/'
    }
  });
  clearCache();
}

export function __clearInfisicalClientCacheForTests() {
  clearCache();
}

export function __setFetchImplementationForTests(fn) {
  fetchImpl = fn;
}
