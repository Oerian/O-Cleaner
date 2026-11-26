/* Minimal Win32 C99 accelerator for O-Cleaner MFT record chunk parsing.
 * Compiles with MSVC (`cl /O2 /MT /LD mft_fast.c /Fe:mft_fast.dll`)
 * or MinGW (`gcc -O2 -shared -static mft_fast.c -o mft_fast.dll`).
 */
#include <stdint.h>
#include <string.h>

#ifdef _WIN32
#define OC_EXPORT __declspec(dllexport)
#else
#define OC_EXPORT
#endif

#define EXT_SLOTS 4096u

typedef struct {
    uint64_t bytes;
    uint32_t count;
    uint32_t hash;
    uint8_t  len;      /* UTF-16LE byte length (<= 32, even) */
    uint8_t  key[32];
} ExtSlot;

static inline uint16_t rd_u16(const uint8_t *p) {
    return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static inline uint32_t rd_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static inline uint64_t rd_u64(const uint8_t *p) {
    return (uint64_t)rd_u32(p) | ((uint64_t)rd_u32(p + 4) << 32);
}

OC_EXPORT int mft_fast_version(void) {
    return 2;
}

/* Try to aggregate UTF-16LE extension into fixed ExtSlot[4096] hash table.
 * Returns 1 if aggregated in C, 0 if caller should fall back to Python.
 */
static int ext_bump_c(ExtSlot *tbl, const uint8_t *name, uint32_t nlen, uint64_t size) {
    if (!tbl) return 0;
    int32_t dot = -1;
    for (int32_t j = (int32_t)nlen - 2; j >= 0; j -= 2) {
        if (name[j] == '.' && name[j + 1] == 0) {
            dot = j;
            break;
        }
    }
    uint8_t kbuf[32];
    uint8_t klen = 0;
    if (dot >= 0) {
        uint32_t start = (uint32_t)dot + 2u;
        uint32_t rem = nlen - start;
        if (rem > 32u) rem = 32u;
        for (uint32_t j = 0; j + 1 < rem; j += 2) {
            uint8_t lo = name[start + j];
            uint8_t hi = name[start + j + 1];
            if (hi != 0) return 0;  /* non-ASCII ext -> Python handles Unicode lower() */
            if (lo >= 'A' && lo <= 'Z') lo = (uint8_t)(lo + 32);
            kbuf[j] = lo;
            kbuf[j + 1] = 0;
        }
        klen = (uint8_t)(rem & ~1u);
    }
    uint32_t h = 2166136261u;
    for (uint32_t j = 0; j < klen; j++) {
        h ^= kbuf[j];
        h *= 16777619u;
    }
    uint32_t mask = EXT_SLOTS - 1u;
    uint32_t idx = h & mask;
    for (uint32_t probe = 0; probe < 64u; probe++) {
        ExtSlot *s = &tbl[idx];
        if (s->count == 0) {
            s->hash = h;
            s->len = klen;
            if (klen) memcpy(s->key, kbuf, klen);
            s->bytes = size;
            s->count = 1;
            return 1;
        }
        if (s->hash == h && s->len == klen && (klen == 0 || memcmp(s->key, kbuf, klen) == 0)) {
            s->bytes += size;
            s->count += 1;
            return 1;
        }
        idx = (idx + 1u) & mask;
    }
    return 0;
}

OC_EXPORT int32_t mft_scan_chunk(
    uint8_t *buf,
    uint32_t usable,
    uint32_t frs_size,
    uint64_t f_floor,
    int32_t ext_stats,
    int32_t with_mtime,
    int64_t *out_parent,
    int64_t *out_size,
    uint8_t *out_flags,
    uint64_t *out_mtime,
    uint32_t *out_name_idx,
    uint32_t *out_name_off,
    uint16_t *out_name_len,
    ExtSlot *out_ext_tbl
) {
    if (!buf || frs_size < 0x40 || usable < frs_size ||
        !out_parent || !out_size || !out_flags ||
        !out_name_idx || !out_name_off || !out_name_len) {
        return -1;
    }

    uint32_t n_recs = usable / frs_size;
    int32_t kept = 0;

    for (uint32_t i = 0; i < n_recs; i++) {
        uint32_t off = i * frs_size;
        uint8_t *rec = buf + off;

        out_parent[i] = 0;
        out_size[i] = 0;
        out_flags[i] = 0;
        if (with_mtime && out_mtime) {
            out_mtime[i] = 0;
        }

        if ((rec[0x16] & 0x01) == 0 || rd_u32(rec) != 0x454C4946u) {
            continue;
        }

        uint32_t usa_off = rd_u16(rec + 4);
        uint32_t usa_cnt = rd_u16(rec + 6);
        if (usa_off > 0 && usa_cnt > 1 && usa_off + usa_cnt * 2 <= frs_size) {
            int ok = 1;
            for (uint32_t u = 1; u < usa_cnt; u++) {
                uint32_t o = u * 512u - 2u;
                if (o + 2u > frs_size) {
                    ok = 0;
                    break;
                }
                rec[o] = rec[usa_off + u * 2u];
                rec[o + 1] = rec[usa_off + u * 2u + 1u];
            }
            if (!ok) {
                continue;
            }
        }

        uint32_t used = rd_u32(rec + 0x18);
        if (used == 0 || used > frs_size) {
            used = frs_size;
        }
        uint32_t a = rd_u16(rec + 0x14);
        int64_t parent = -1;
        uint32_t name_off = 0;
        uint16_t name_len = 0;
        uint64_t size = 0;
        uint8_t is_dir = 0;
        uint64_t mtime = 0;
        int corrupt = 0;

        while (a >= 0x10 && a + 16u <= frs_size && a < used) {
            uint32_t a_type = rd_u32(rec + a);
            uint32_t a_len = rd_u32(rec + a + 4);
            if (a_type == 0xFFFFFFFFu || a_len == 0) {
                break;
            }
            if (a_len < 24u || a + a_len > frs_size || (a_len & 7u) != 0) {
                corrupt = 1;
                break;
            }
            uint8_t nonres = rec[a + 8];
            if (a_type == 0x30u && nonres == 0) {
                if (name_len == 0) {
                    uint32_t c = a + rd_u16(rec + a + 0x14);
                    if (c >= 0x42u && c + 0x42u <= frs_size) {
                        parent = (int64_t)(rd_u64(rec + c) & 0xFFFFFFFFFFFFULL);
                        uint8_t nl = rec[c + 0x40];
                        uint8_t ns = rec[c + 0x41];
                        if (ns != 2 && nl > 0 && c + 0x42u + (uint32_t)nl * 2u <= frs_size) {
                            name_off = off + c + 0x42u;
                            name_len = (uint16_t)nl * 2u;
                        }
                    }
                }
            } else if (a_type == 0x80u) {
                if (nonres) {
                    if (size == 0 && a + 0x40u <= frs_size) {
                        size = rd_u64(rec + a + 0x38);
                    }
                } else {
                    size = rd_u32(rec + a + 0x10);
                }
                if (parent >= 0) {
                    break;
                }
            } else if (a_type == 0x90u) {
                is_dir = 1;
            } else if (a_type == 0x10u && with_mtime && mtime == 0 && nonres == 0) {
                uint32_t c = a + rd_u16(rec + a + 0x14);
                if (c >= 0x18u && c + 0x18u <= frs_size) {
                    mtime = rd_u64(rec + c + 8);
                }
            }
            a += a_len;
        }

        if (corrupt || parent < 0) {
            continue;
        }

        out_parent[i] = (parent < (1LL << 47)) ? parent : 0;
        out_size[i] = (int64_t)size;
        out_flags[i] = is_dir ? 1 : 0;
        if (with_mtime && out_mtime) {
            out_mtime[i] = mtime;
        }

        if (name_len > 0) {
            int need_py = is_dir || (size >= f_floor) || (parent == 5);
            uint16_t ext_ok = 0;
            if (!is_dir && ext_stats && out_ext_tbl) {
                if (ext_bump_c(out_ext_tbl, buf + name_off, name_len, size)) {
                    ext_ok = 0x8000u;
                } else {
                    need_py = 1;
                }
            }
            if (need_py) {
                out_name_idx[kept] = i;
                out_name_off[kept] = name_off;
                out_name_len[kept] = (uint16_t)(name_len | ext_ok);
                kept++;
            }
        }
    }
    return kept;
}
