// Mirrors backend/app/auth.py: WEAK_PASSWORDS and validate_registration.
// Python 3.11 str.isdigit() uses Unicode 14 Numeric_Type=Decimal or Digit.
// Freeze that set rather than using JS \d (ASCII only) or Number (also fractions).
const PYTHON_DIGITS = /^[\u{30}-\u{39}\u{B2}-\u{B3}\u{B9}\u{660}-\u{669}\u{6F0}-\u{6F9}\u{7C0}-\u{7C9}\u{966}-\u{96F}\u{9E6}-\u{9EF}\u{A66}-\u{A6F}\u{AE6}-\u{AEF}\u{B66}-\u{B6F}\u{BE6}-\u{BEF}\u{C66}-\u{C6F}\u{CE6}-\u{CEF}\u{D66}-\u{D6F}\u{DE6}-\u{DEF}\u{E50}-\u{E59}\u{ED0}-\u{ED9}\u{F20}-\u{F29}\u{1040}-\u{1049}\u{1090}-\u{1099}\u{1369}-\u{1371}\u{17E0}-\u{17E9}\u{1810}-\u{1819}\u{1946}-\u{194F}\u{19D0}-\u{19DA}\u{1A80}-\u{1A89}\u{1A90}-\u{1A99}\u{1B50}-\u{1B59}\u{1BB0}-\u{1BB9}\u{1C40}-\u{1C49}\u{1C50}-\u{1C59}\u{2070}\u{2074}-\u{2079}\u{2080}-\u{2089}\u{2460}-\u{2468}\u{2474}-\u{247C}\u{2488}-\u{2490}\u{24EA}\u{24F5}-\u{24FD}\u{24FF}\u{2776}-\u{277E}\u{2780}-\u{2788}\u{278A}-\u{2792}\u{A620}-\u{A629}\u{A8D0}-\u{A8D9}\u{A900}-\u{A909}\u{A9D0}-\u{A9D9}\u{A9F0}-\u{A9F9}\u{AA50}-\u{AA59}\u{ABF0}-\u{ABF9}\u{FF10}-\u{FF19}\u{104A0}-\u{104A9}\u{10A40}-\u{10A43}\u{10D30}-\u{10D39}\u{10E60}-\u{10E68}\u{11052}-\u{1105A}\u{11066}-\u{1106F}\u{110F0}-\u{110F9}\u{11136}-\u{1113F}\u{111D0}-\u{111D9}\u{112F0}-\u{112F9}\u{11450}-\u{11459}\u{114D0}-\u{114D9}\u{11650}-\u{11659}\u{116C0}-\u{116C9}\u{11730}-\u{11739}\u{118E0}-\u{118E9}\u{11950}-\u{11959}\u{11C50}-\u{11C59}\u{11D50}-\u{11D59}\u{11DA0}-\u{11DA9}\u{16A60}-\u{16A69}\u{16AC0}-\u{16AC9}\u{16B50}-\u{16B59}\u{1D7CE}-\u{1D7FF}\u{1E140}-\u{1E149}\u{1E2F0}-\u{1E2F9}\u{1E950}-\u{1E959}\u{1F100}-\u{1F10A}\u{1FBF0}-\u{1FBF9}]+$(?![\s\S])/u;
const WEAK_PASSWORDS = new Set([
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "qwertyuiop", "admin123", "admin12345", "welcome123", "iloveyou",
    "letmein123", "abc12345", "abcd1234", "123456abc", "11111111", "88888888",
    "00000000", "asdfghjk", "changeme", "passw0rd", "administrator",
]);

export function registrationPasswordRules(username: string, password: string) {
  const length = Array.from(password).length;
  return [
    { id: 'length', label: '8–64 位字符', satisfied: length >= 8 && length <= 64, error: '密码须为 8–64 位。' },
    { id: 'different', label: '不能与用户名相同（不区分大小写）', satisfied: password.toLowerCase() !== username.toLowerCase(), error: '密码不能与用户名相同。' },
    { id: 'digits', label: '不能为纯数字（含全角等 Unicode 数字）', satisfied: !PYTHON_DIGITS.test(password), error: '密码不能为纯数字。' },
    { id: 'common', label: '不能使用常见弱口令（如 password123、qwerty123，不区分大小写）', satisfied: !WEAK_PASSWORDS.has(password.toLowerCase()), error: '密码过于常见，请使用更安全的密码。' },
  ];
}

export function validateCredentials(username: string, password: string, mode: 'login' | 'register' = 'login'): string | undefined {
  const length = Array.from(password).length;
  if (mode === 'register' && length > 64) return '密码须为 8–64 位。';
  if (username.length < 3 || username.length > 20 || /[^A-Za-z0-9_]/.test(username)) return '用户名须为 3–20 位，仅含字母、数字或下划线。';
  if (mode === 'register') return registrationPasswordRules(username, password).find(rule => !rule.satisfied)?.error;
  if (length > 64) return '密码最多 64 位。';
}
