// main/wb_adpcm.c —— IMA/DVI 4bit ADPCM 编码器实现。
#include "wb_adpcm.h"

// IMA 标准步长表（89 项）
static const int32_t STEP[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41,
    45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209,
    230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876,
    963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493,
    10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086,
    29794, 32767
};

// 3bit delta(0..7) -> 索引增量
static const int32_t IDX[8] = { -1, -1, -1, -1, 2, 4, 6, 8 };

static inline int32_t clamp16(int32_t v)
{
    if (v < -32768) return -32768;
    if (v > 32767) return 32767;
    return v;
}

void wb_adpcm_reset(wb_adpcm_t *st, int16_t seed)
{
    st->pred = seed;
    st->index = 0;
}

// 重构出的差值幅度。编解码必须用同一个公式，预测器才能逐采样对齐。
static inline int32_t step_diff(int32_t step, int nib)
{
    int32_t d = step >> 3;
    if (nib & 4) d += step;
    if (nib & 2) d += step >> 1;
    if (nib & 1) d += step >> 2;
    return d;
}

void wb_adpcm_encode(wb_adpcm_t *st, const int16_t *in, size_t n, uint8_t *out)
{
    size_t o = 0;
    uint8_t packed = 0;

    for (size_t i = 0; i < n; i++) {
        int32_t step = STEP[st->index];
        int32_t diff = (int32_t)in[i] - st->pred;
        int     sign = 0;

        if (diff < 0) {
            sign = 8;
            diff = -diff;
        }

        int nib = 0;
        if (diff >= step) {
            nib = 4;
            diff -= step;
        }
        step >>= 1;
        if (diff >= step) {
            nib |= 2;
            diff -= step;
        }
        step >>= 1;
        if (diff >= step) {
            nib |= 1;
        }

        nib |= sign;

        // 预测器更新：与解码端完全同式
        int32_t vp = step_diff(STEP[st->index], nib & 7);
        st->pred = sign ? (st->pred - vp) : (st->pred + vp);
        st->pred = clamp16(st->pred);

        st->index += IDX[nib & 7];
        if (st->index < 0) st->index = 0;
        else if (st->index > 88) st->index = 88;

        // 低半字节先出
        if ((i & 1) == 0) {
            packed = (uint8_t)(nib & 0x0F);
        } else {
            packed |= (uint8_t)((nib & 0x0F) << 4);
            out[o++] = packed;
        }
    }

    // n 为奇数时把最后一个半字节单独补出（本工程调用点保证偶数，仍做保护）
    if (n & 1) out[o++] = packed;
}
