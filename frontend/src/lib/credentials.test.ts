import { resolve } from 'node:path';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { registrationPasswordRules, validateCredentials } from './credentials';

describe('registration rules matching backend/app/auth.py', () => {
  it.each([
    ['abc', 'a'.repeat(7), '8–64'], ['abc', 'a'.repeat(65), '8–64'],
    ['Same_User', 'sAME_uSER', '用户名相同'], ['abc', '12345678', '纯数字'],
    ['abc', '１２３４５６７８', '纯数字'], ['abc', '١٢٣٤٥٦٧٨', '纯数字'],
    ['abc', '²'.repeat(8), '纯数字'], ['abc', '①'.repeat(8), '纯数字'],
    ['abc', String.fromCodePoint(0x16ac0).repeat(8), '纯数字'], // Unicode 14 Tangsa
  ])('rejects %s / password rule %s', (username, password, reason) => {
    expect(validateCredentials(username, password, 'register')).toContain(reason);
  });
  it.each([
    'a'.repeat(8), 'a'.repeat(64), '😀'.repeat(8), '8¹½abcde', '12345678\n',
    '⑳'.repeat(8), // Numeric value twenty is not Python isdigit().
    String.fromCodePoint(0x1e4f0).repeat(8), // Unicode 15 digits are not digits in Python 3.11.
  ])('accepts valid boundary and Unicode passwords', password => {
    expect(validateCredentials('abc', password, 'register')).toBeUndefined();
  });
  it('keeps every backend weak password forbidden, including mixed case', () => {
    const backend = readFileSync(resolve(process.cwd(), '../backend/app/auth.py'), 'utf8');
    const declaration = backend.match(/WEAK_PASSWORDS = frozenset\(\{([\s\S]*?)\}\)/)?.[1];
    expect(declaration).toBeDefined();
    const passwords = [...declaration!.matchAll(/"([^"]+)"/g)].map(match => match[1]);
    expect(passwords.length).toBeGreaterThan(20);
    for (const password of passwords) {
      expect(registrationPasswordRules('unique_user', password.toUpperCase()).find(rule => rule.id === 'common')?.satisfied).toBe(false);
      expect(validateCredentials('unique_user', password.toUpperCase(), 'register')).toBeDefined();
    }
  });
  it('enforces full ASCII username matching and inclusive boundaries', () => {
    for (const name of ['ab', 'a'.repeat(21), 'abc\n', '用户名', 'abc-']) {
      expect(validateCredentials(name, 'Unique_pass!', 'register')).toContain('用户名');
    }
    for (const name of ['Ab_', 'a'.repeat(20)]) {
      expect(validateCredentials(name, 'Unique_pass!', 'register')).toBeUndefined();
    }
  });
  it('keeps registration strength rules out of login', () => {
    expect(validateCredentials('password', 'password', 'login')).toBeUndefined();
    expect(validateCredentials('abc', '12345678', 'login')).toBeUndefined();
  });
});
