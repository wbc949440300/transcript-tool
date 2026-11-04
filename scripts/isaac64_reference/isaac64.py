MASK = (1 << 64) - 1
GOLDEN = 0x9E3779B97F4A7C13


def _mix(s):
    a, b, c, d, e, f, g, h = s
    a = (a - e) & MASK; f ^= h >> 9;       h = (h + a) & MASK
    b = (b - f) & MASK; g ^= (a << 9) & MASK; a = (a + b) & MASK
    c = (c - g) & MASK; h ^= b >> 23;      b = (b + c) & MASK
    d = (d - h) & MASK; a ^= (c << 15) & MASK; c = (c + d) & MASK
    e = (e - a) & MASK; b ^= d >> 14;      d = (d + e) & MASK
    f = (f - b) & MASK; c ^= (e << 20) & MASK; e = (e + f) & MASK
    g = (g - c) & MASK; d ^= f >> 17;      f = (f + g) & MASK
    h = (h - d) & MASK; e ^= (g << 14) & MASK; g = (g + h) & MASK
    s[:] = [a & MASK, b & MASK, c & MASK, d & MASK,
            e & MASK, f & MASK, g & MASK, h & MASK]


class Isaac64:
    """ISAAC64 流密钥生成器；与微信视频号解密所用算法逐位一致。"""

    def __init__(self, seed_key: int):
        self.cnt = 255
        self.aa = self.bb = self.cc = 0
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.seed[0] = seed_key & MASK

        st = [GOLDEN] * 8
        for _ in range(4):
            _mix(st)
        for i in range(0, 256, 8):
            for j in range(8):
                st[j] = (st[j] + self.seed[i + j]) & MASK
            _mix(st)
            self.mm[i:i + 8] = st
        for i in range(0, 256, 8):
            for j in range(8):
                st[j] = (st[j] + self.mm[i + j]) & MASK
            _mix(st)
            self.mm[i:i + 8] = st
        self._isaac64()

    def _isaac64(self):
        self.cc = (self.cc + 1) & MASK
        self.bb = (self.bb + self.cc) & MASK
        aa, bb, mm, seed = self.aa, self.bb, self.mm, self.seed
        for i in range(256):
            r = i & 3
            if r == 0:
                aa = (~(aa ^ ((aa << 21) & MASK))) & MASK
            elif r == 1:
                aa ^= aa >> 5
            elif r == 2:
                aa ^= (aa << 12) & MASK
            else:
                aa ^= aa >> 33
            aa = (aa + mm[(i + 128) & 255]) & MASK
            x = mm[i]
            y = (mm[(x >> 3) & 255] + aa + bb) & MASK
            mm[i] = y
            bb = (mm[(y >> 11) & 255] + x) & MASK
            seed[i] = bb
        self.aa, self.bb = aa, bb

    def next_u64(self) -> int:
        result = self.seed[self.cnt]
        if self.cnt == 0:
            self._isaac64()
            self.cnt = 255
        else:
            self.cnt -= 1
        return result

    def keystream(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            out += self.next_u64().to_bytes(8, "big")
        return bytes(out[:n])


def decrypt(data: bytes, enc_len: int, key: int) -> bytes:
    """只解密前 enc_len 字节（mp4 头尾为明文），与微信客户端行为一致。"""
    enc_len = min(enc_len, len(data))
    ks = Isaac64(key).keystream(enc_len)
    buf = bytearray(data[:enc_len])
    for i in range(enc_len):
        buf[i] ^= ks[i]
    return bytes(buf) + data[enc_len:]


if __name__ == "__main__":
    vectors = {
        1: [16257666806172921645, 12079090740189436754, 11577349684372483860,
            8265070477432972399, 17204147743807346879, 10840387247671765879],
        12345: [16749476496145720041, 9916843529103186837, 11968398467845635923,
                9337830697406450407, 14531341148415096772, 14306891581045654757],
        0x1234567890ABCDEF: [10992955994126971164, 5417976648393922963,
                             13415880611307289996, 17158371038328813426,
                             18189446183658915504, 5257061639556767708],
    }
    ok = True
    for key, expect in vectors.items():
        got = [Isaac64(key).next_u64() for _ in range(1)]
        r = Isaac64(key)
        got = [r.next_u64() for _ in range(6)]
        same = got == expect
        ok &= same
        print(("PASS" if same else "FAIL"), f"key={key}", got if not same else "")
    print("ALL_PASS" if ok else "MISMATCH")
