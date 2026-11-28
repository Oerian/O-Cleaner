/* Minimal Win32 C99 accelerator for O-Cleaner MFT record chunk parsing.
 * Compiles with MSVC (`cl /O2 /MT /LD mft_fast.c /Fe:mft_fast.dll`)
 * or MinGW (`gcc -O2 -shared -static mft_fast.c -o mft_fast.dll`).
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
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
    return 4;
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
            if (hi != 0 || lo >= 0x80) return 0;  /* non-ASCII ext -> Python handles Unicode lower() */
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
        if (usa_off < 0x2Au || usa_cnt <= 1u || usa_cnt > (frs_size / 512u) + 1u ||
            usa_off + usa_cnt * 2u > frs_size) {
            continue;
        }
        {
            uint8_t usn0 = rec[usa_off];
            uint8_t usn1 = rec[usa_off + 1u];
            int ok = 1;
            for (uint32_t u = 1; u < usa_cnt; u++) {
                uint32_t o = u * 512u - 2u;
                if (o + 2u > frs_size || rec[o] != usn0 || rec[o + 1u] != usn1) {
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
        uint8_t is_dir = (rec[0x16] & 0x02) ? 1 : 0;
        uint64_t mtime = 0;
        int corrupt = 0;

        while (a >= 0x10u && a + 16u <= used && a + 16u <= frs_size) {
            uint32_t a_type = rd_u32(rec + a);
            uint32_t a_len = rd_u32(rec + a + 4);
            if (a_type == 0xFFFFFFFFu || a_len == 0) {
                break;
            }
            if (a_len < 24u || a_len > frs_size - a || (a_len & 7u) != 0) {
                corrupt = 1;
                break;
            }
            uint32_t a_end = a + a_len;
            uint8_t nonres = rec[a + 8];
            uint8_t attr_nlen = rec[a + 9];
            if (a_type == 0x30u && nonres == 0) {
                if (name_len == 0) {
                    uint32_t c = a + rd_u16(rec + a + 0x14);
                    if (c >= a + 0x18u && c + 0x42u <= a_end) {
                        parent = (int64_t)(rd_u64(rec + c) & 0xFFFFFFFFFFFFULL);
                        uint8_t nl = rec[c + 0x40];
                        uint8_t ns = rec[c + 0x41];
                        if (ns != 2 && nl > 0 && (uint32_t)nl * 2u <= a_end - (c + 0x42u)) {
                            name_off = off + c + 0x42u;
                            name_len = (uint16_t)nl * 2u;
                        }
                    }
                }
            } else if (a_type == 0x80u && attr_nlen == 0) {
                if (nonres) {
                    if (size == 0 && a_len >= 0x40u && rd_u64(rec + a + 0x10) == 0) {
                        size = rd_u64(rec + a + 0x30);
                    }
                } else if (a_len >= 0x18u) {
                    size = rd_u32(rec + a + 0x10);
                }
                if (size > (uint64_t)INT64_MAX) {
                    size = (uint64_t)INT64_MAX;
                }
                if (parent >= 0 && is_dir) {
                    break;
                }
            } else if (a_type == 0x90u) {
                is_dir = 1;
            } else if (a_type == 0x10u && with_mtime && mtime == 0 && nonres == 0) {
                uint32_t c = a + rd_u16(rec + a + 0x14);
                if (c >= a + 0x18u && c + 0x18u <= a_end) {
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

/* Aggregate directory sizes & BFS reachability in C (O(n_recs), zero Python loops).
 * Returns count of want FRNs written to out_want_frns, or -1 on error/oom.
 */
OC_EXPORT int32_t mft_agg_tree(
    int32_t n_recs,
    const int64_t *parent,
    const int64_t *size,
    const uint8_t *flags,
    uint64_t floor_bytes,
    int64_t *out_dir_total,
    uint8_t *out_in_tree,
    int32_t *out_want_frns,
    int32_t max_want
) {
    if (n_recs <= 5 || n_recs > 0x0FFFFFFF || !parent || !size || !flags ||
        !out_dir_total || !out_in_tree || !out_want_frns || max_want <= 0) {
        return -1;
    }
    memset(out_dir_total, 0, (size_t)n_recs * sizeof(int64_t));
    memset(out_in_tree, 0, (size_t)n_recs * sizeof(uint8_t));

    int32_t *head = (int32_t *)malloc((size_t)n_recs * sizeof(int32_t));
    int32_t *next_sib = (int32_t *)malloc((size_t)n_recs * sizeof(int32_t));
    int32_t *queue = (int32_t *)malloc((size_t)n_recs * sizeof(int32_t));
    if (!head || !next_sib || !queue) {
        free(head);
        free(next_sib);
        free(queue);
        return -1;
    }
    for (int32_t i = 0; i < n_recs; i++) {
        head[i] = -1;
        next_sib[i] = -1;
    }

    for (int32_t i = 0; i < n_recs; i++) {
        int64_t p64 = parent[i];
        if (p64 <= 0 || p64 >= n_recs) continue;
        int32_t p = (int32_t)p64;
        if (flags[i] & 1) {
            if (i != 5 && p != i) {
                next_sib[i] = head[p];
                head[p] = i;
            }
        } else {
            int64_t sz = size[i];
            if (sz > 0) {
                out_dir_total[p] += sz;
            }
        }
    }

    int32_t q_head = 0, q_tail = 0;
    out_in_tree[5] = 1;
    queue[q_tail++] = 5;
    while (q_head < q_tail) {
        int32_t cur = queue[q_head++];
        for (int32_t c = head[cur]; c >= 0; c = next_sib[c]) {
            if (!out_in_tree[c]) {
                out_in_tree[c] = 1;
                queue[q_tail++] = c;
            }
        }
    }

    for (int32_t k = q_tail - 1; k >= 1; k--) {
        int32_t d = queue[k];
        int32_t p = (int32_t)parent[d];
        if (p > 0 && p < n_recs && out_in_tree[p]) {
            out_dir_total[p] += out_dir_total[d];
        }
    }

    int32_t want_n = 0;
    out_want_frns[want_n++] = 5;
    for (int32_t k = 1; k < q_tail; k++) {
        int32_t d = queue[k];
        if ((uint64_t)out_dir_total[d] >= floor_bytes || parent[d] == 5) {
            if (want_n < max_want) {
                out_want_frns[want_n++] = d;
            }
        }
    }

    free(head);
    free(next_sib);
    free(queue);
    return want_n;
}

#ifdef _WIN32
typedef struct {
    HANDLE h_vol;
    uint32_t bpc;
    uint32_t frs_size;
    uint32_t chunk_bytes;
    int64_t *runs_lcn;
    int64_t *runs_len;
    int32_t n_runs;
    uint8_t *buf[2];
    uint32_t usable[2];
    uint8_t tail_buf[4096];
    int eof;
    HANDLE ev_filled[2];
    HANDLE ev_free[2];
    HANDLE th;
    volatile LONG stop;
    int32_t cur_slot;
    int has_prev;
} MftReader;

static DWORD WINAPI mft_reader_proc(LPVOID arg) {
    MftReader *rd = (MftReader *)arg;
    int32_t slot = 0;
    uint32_t held = 0;
    for (int32_t r = 0; r < rd->n_runs && !rd->stop; r++) {
        int64_t lcn = rd->runs_lcn[r];
        int64_t clen = rd->runs_len[r];
        if (lcn <= 0 || clen <= 0) continue;
        int64_t pos = lcn * (int64_t)rd->bpc;
        int64_t left = clen * (int64_t)rd->bpc;
        while (left > 0 && !rd->stop) {
            WaitForSingleObject(rd->ev_free[slot], INFINITE);
            if (rd->stop) break;
            if (held > 0) {
                memcpy(rd->buf[slot], rd->tail_buf, held);
            }
            DWORD want = (DWORD)((left < (int64_t)rd->chunk_bytes) ? left : (int64_t)rd->chunk_bytes);
            LARGE_INTEGER li;
            li.QuadPart = pos;
            if (!SetFilePointerEx(rd->h_vol, li, NULL, FILE_BEGIN)) {
                held = 0;
                SetEvent(rd->ev_free[slot]);
                break;
            }
            DWORD got = 0;
            if (!ReadFile(rd->h_vol, rd->buf[slot] + held, want, &got, NULL) || got == 0) {
                held = 0;
                SetEvent(rd->ev_free[slot]);
                break;
            }
            uint32_t total = held + (uint32_t)got;
            uint32_t use = total - (total % rd->frs_size);
            held = total - use;
            if (held > 0 && held <= sizeof(rd->tail_buf)) {
                memcpy(rd->tail_buf, rd->buf[slot] + use, held);
            } else {
                held = 0;
            }
            pos += (int64_t)got;
            left -= (int64_t)got;
            if (use > 0) {
                rd->usable[slot] = use;
                SetEvent(rd->ev_filled[slot]);
                slot ^= 1;
            } else {
                SetEvent(rd->ev_free[slot]);
            }
        }
    }
    if (rd->stop) return 0;
    WaitForSingleObject(rd->ev_free[slot], INFINITE);
    if (rd->stop) return 0;
    rd->usable[slot] = 0;
    SetEvent(rd->ev_filled[slot]);
    return 0;
}

OC_EXPORT void *mft_reader_open(
    void *h_vol,
    uint32_t bpc,
    uint32_t frs_size,
    uint32_t chunk_bytes,
    const int64_t *runs_lcn,
    const int64_t *runs_len,
    int32_t n_runs
) {
    if (!h_vol || (HANDLE)h_vol == INVALID_HANDLE_VALUE || bpc == 0 ||
        frs_size < 0x40 || frs_size > 4096 ||
        chunk_bytes < frs_size || chunk_bytes > (0x7FFFFFFFu - frs_size) ||
        !runs_lcn || !runs_len || n_runs <= 0 || n_runs > 0x0FFFFFFF) {
        return NULL;
    }
    MftReader *rd = (MftReader *)calloc(1, sizeof(MftReader));
    if (!rd) return NULL;
    rd->h_vol = (HANDLE)h_vol;
    rd->bpc = bpc;
    rd->frs_size = frs_size;
    rd->chunk_bytes = chunk_bytes;
    rd->n_runs = n_runs;
    rd->runs_lcn = (int64_t *)malloc((size_t)n_runs * sizeof(int64_t));
    rd->runs_len = (int64_t *)malloc((size_t)n_runs * sizeof(int64_t));
    rd->buf[0] = (uint8_t *)malloc((size_t)chunk_bytes + frs_size);
    rd->buf[1] = (uint8_t *)malloc((size_t)chunk_bytes + frs_size);
    if (!rd->runs_lcn || !rd->runs_len || !rd->buf[0] || !rd->buf[1]) {
        free(rd->runs_lcn); free(rd->runs_len);
        free(rd->buf[0]); free(rd->buf[1]); free(rd);
        return NULL;
    }
    memcpy(rd->runs_lcn, runs_lcn, (size_t)n_runs * sizeof(int64_t));
    memcpy(rd->runs_len, runs_len, (size_t)n_runs * sizeof(int64_t));
    for (int i = 0; i < 2; i++) {
        rd->ev_filled[i] = CreateEventW(NULL, FALSE, FALSE, NULL);
        rd->ev_free[i] = CreateEventW(NULL, FALSE, TRUE, NULL);
        if (!rd->ev_filled[i] || !rd->ev_free[i]) {
            for (int j = 0; j <= i; j++) {
                if (rd->ev_filled[j]) CloseHandle(rd->ev_filled[j]);
                if (rd->ev_free[j]) CloseHandle(rd->ev_free[j]);
            }
            free(rd->runs_lcn); free(rd->runs_len);
            free(rd->buf[0]); free(rd->buf[1]); free(rd);
            return NULL;
        }
    }
    rd->th = CreateThread(NULL, 0, mft_reader_proc, rd, 0, NULL);
    if (!rd->th) {
        for (int i = 0; i < 2; i++) { CloseHandle(rd->ev_filled[i]); CloseHandle(rd->ev_free[i]); }
        free(rd->runs_lcn); free(rd->runs_len);
        free(rd->buf[0]); free(rd->buf[1]); free(rd);
        return NULL;
    }
    return rd;
}

OC_EXPORT int32_t mft_reader_next(void *ctx, uint64_t *out_ptr, uint32_t *out_usable) {
    MftReader *rd = (MftReader *)ctx;
    if (!rd || !out_ptr || !out_usable) return -1;
    if (rd->eof) return 0;
    if (rd->has_prev) {
        SetEvent(rd->ev_free[rd->cur_slot ^ 1]);
        rd->has_prev = 0;
    }
    WaitForSingleObject(rd->ev_filled[rd->cur_slot], INFINITE);
    uint32_t use = rd->usable[rd->cur_slot];
    if (use == 0) {
        rd->eof = 1;
        return 0;
    }
    *out_ptr = (uint64_t)(uintptr_t)rd->buf[rd->cur_slot];
    *out_usable = use;
    rd->has_prev = 1;
    rd->cur_slot ^= 1;
    return 1;
}

OC_EXPORT void mft_reader_close(void *ctx) {
    MftReader *rd = (MftReader *)ctx;
    if (!rd) return;
    InterlockedExchange(&rd->stop, 1);
    SetEvent(rd->ev_free[0]);
    SetEvent(rd->ev_free[1]);
    WaitForSingleObject(rd->th, INFINITE);
    CloseHandle(rd->th);
    for (int i = 0; i < 2; i++) {
        CloseHandle(rd->ev_filled[i]);
        CloseHandle(rd->ev_free[i]);
        free(rd->buf[i]);
    }
    free(rd->runs_lcn);
    free(rd->runs_len);
    free(rd);
}
#endif
