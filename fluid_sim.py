#!/usr/bin/env python3
"""
2D Fluid Simulator — Jos Stam stable fluids

Controls:
  Left drag   : add dye + velocity
  Right drag  : add dye in a second colour
  C           : cycle colour themes (fire / ocean / neon)
  R           : reset
  ESC         : quit

Requirements:
  pip install pygame numpy
"""

import sys
import numpy as np
import pygame
from pygame.locals import QUIT, KEYDOWN, K_ESCAPE, K_r, K_c

# ── Config ────────────────────────────────────────────────────────────────────
N      = 120        # grid cells per axis
SCALE  = 6          # pixels per cell
DT     = 0.15       # time step
DIFF   = 8e-6       # dye diffusion rate
VISC   = 5e-7       # fluid viscosity
ITER   = 10         # Gauss-Seidel iterations
FADE   = 0.993      # density fade per frame
FPS    = 60

WIN_W = N * SCALE
WIN_H = N * SCALE
SZ    = N + 2       # grid size including ghost boundary cells


# ── Helpers ───────────────────────────────────────────────────────────────────

def mk():
    return np.zeros((SZ, SZ), dtype=np.float64)


def set_bnd(b: int, x: np.ndarray) -> None:
    """Reflect values at the four boundary walls."""
    # Left / right walls
    x[0,    1:N+1] = -x[1,   1:N+1] if b == 1 else x[1,   1:N+1]
    x[N+1,  1:N+1] = -x[N,   1:N+1] if b == 1 else x[N,   1:N+1]
    # Top / bottom walls
    x[1:N+1, 0   ] = -x[1:N+1, 1  ] if b == 2 else x[1:N+1, 1  ]
    x[1:N+1, N+1 ] = -x[1:N+1, N  ] if b == 2 else x[1:N+1, N  ]
    # Corners
    x[0,   0  ] = 0.5 * (x[1,  0  ] + x[0,   1])
    x[0,   N+1] = 0.5 * (x[1,  N+1] + x[0,   N])
    x[N+1, 0  ] = 0.5 * (x[N,  0  ] + x[N+1, 1])
    x[N+1, N+1] = 0.5 * (x[N,  N+1] + x[N+1, N])


def lin_solve(b: int, x: np.ndarray, x0: np.ndarray,
              a: float, c: float) -> None:
    inv_c = 1.0 / c
    for _ in range(ITER):
        x[1:N+1, 1:N+1] = (x0[1:N+1, 1:N+1] + a * (
            x[0:N,   1:N+1] + x[2:N+2, 1:N+1] +
            x[1:N+1, 0:N  ] + x[1:N+1, 2:N+2]
        )) * inv_c
        set_bnd(b, x)


def diffuse(b: int, x: np.ndarray, x0: np.ndarray, diff: float) -> None:
    a = DT * diff * N * N
    lin_solve(b, x, x0, a, 1 + 4 * a)


def project(vx: np.ndarray, vy: np.ndarray,
            p: np.ndarray, div: np.ndarray) -> None:
    h = 1.0 / N
    div[1:N+1, 1:N+1] = -0.5 * h * (
        vx[2:N+2, 1:N+1] - vx[0:N,   1:N+1] +
        vy[1:N+1, 2:N+2] - vy[1:N+1, 0:N  ]
    )
    p[1:N+1, 1:N+1] = 0
    set_bnd(0, div)
    set_bnd(0, p)
    lin_solve(0, p, div, 1, 4)
    vx[1:N+1, 1:N+1] -= 0.5 * (p[2:N+2, 1:N+1] - p[0:N,   1:N+1]) / h
    vy[1:N+1, 1:N+1] -= 0.5 * (p[1:N+1, 2:N+2] - p[1:N+1, 0:N  ]) / h
    set_bnd(1, vx)
    set_bnd(2, vy)


def advect(b: int, d: np.ndarray, d0: np.ndarray,
           u: np.ndarray, v: np.ndarray) -> None:
    dt0 = DT * N
    i = np.arange(1, N+1)
    j = np.arange(1, N+1)
    ii, jj = np.meshgrid(i, j, indexing='ij')

    x = np.clip(ii - dt0 * u[1:N+1, 1:N+1], 0.5, N + 0.5)
    y = np.clip(jj - dt0 * v[1:N+1, 1:N+1], 0.5, N + 0.5)

    i0 = x.astype(int);  i1 = np.clip(i0 + 1, 0, N+1)
    j0 = y.astype(int);  j1 = np.clip(j0 + 1, 0, N+1)
    i0 = np.clip(i0, 0, N+1)
    j0 = np.clip(j0, 0, N+1)

    s1 = x - i0;  s0 = 1 - s1
    t1 = y - j0;  t0 = 1 - t1

    d[1:N+1, 1:N+1] = (
        s0 * (t0 * d0[i0, j0] + t1 * d0[i0, j1]) +
        s1 * (t0 * d0[i1, j0] + t1 * d0[i1, j1])
    )
    set_bnd(b, d)


# ── Colour maps ───────────────────────────────────────────────────────────────

COLOUR_MODES = ["fire", "ocean", "neon"]

def density_to_rgb(d: np.ndarray, mode: str) -> np.ndarray:
    """Map density [0, 1] → RGB uint8 array (N, N, 3)."""
    d = np.clip(d, 0, 1)
    if mode == "fire":
        r = np.clip(d * 2.5,       0, 1)
        g = np.clip(d * 2.5 - 0.8, 0, 1) * 0.85
        b = np.clip(d * 2.5 - 2.0, 0, 1) * 0.5
    elif mode == "ocean":
        r = np.clip(d * 2 - 1.2,   0, 1) * 0.6
        g = np.clip(d * 1.8 - 0.3, 0, 1) * 0.9
        b = np.clip(d * 1.2 + 0.1, 0, 1)
    else:  # neon
        r = np.clip(d * 3 - 2,     0, 1) * 0.9
        g = np.clip(d * 2,         0, 1)
        b = np.clip(d * 3 - 1,     0, 1) * 0.7
    return (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)


# ── Simulation state ──────────────────────────────────────────────────────────

def make_state():
    return {
        "den":  mk(), "den0": mk(),
        "den2": mk(), "den20": mk(),   # second colour channel (right-click)
        "vx":   mk(), "vx0":  mk(),
        "vy":   mk(), "vy0":  mk(),
    }


def step(s: dict) -> None:
    # Velocity
    diffuse(1, s["vx0"], s["vx"], VISC)
    diffuse(2, s["vy0"], s["vy"], VISC)
    project(s["vx0"], s["vy0"], s["vx"], s["vy"])
    advect(1, s["vx"], s["vx0"], s["vx0"], s["vy0"])
    advect(2, s["vy"], s["vy0"], s["vx0"], s["vy0"])
    project(s["vx"], s["vy"], s["vx0"], s["vy0"])

    # Dye channels
    for dn, dn0 in [("den", "den0"), ("den2", "den20")]:
        diffuse(0, s[dn0], s[dn], DIFF)
        advect(0, s[dn], s[dn0], s["vx"], s["vy"])
        s[dn] *= FADE

    s["vx"] *= 0.9998
    s["vy"] *= 0.9998


def render(screen: pygame.Surface, s: dict, mode: str) -> None:
    d1 = s["den" ][1:N+1, 1:N+1]
    d2 = s["den2"][1:N+1, 1:N+1]

    rgb1 = density_to_rgb(d1, mode).astype(np.float32)

    # Blend second colour (right-click) as a complementary hue
    d2c = np.clip(d2, 0, 1)[..., np.newaxis]
    complement = np.array([255 - rgb1[..., 0],
                            rgb1[..., 1] * 0.3,
                            255 - rgb1[..., 2]], dtype=np.float32).transpose(1, 2, 0)
    rgb = np.clip(rgb1 * (1 - d2c) + complement * d2c, 0, 255).astype(np.uint8)

    surf  = pygame.surfarray.make_surface(rgb)
    pygame.transform.scale(surf, (WIN_W, WIN_H), screen)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption(
        "Fluid Sim  |  LMB/RMB = add dye  |  C = colour  |  R = reset  |  ESC = quit"
    )
    clock  = pygame.font.SysFont("monospace", 13)
    ticker = pygame.time.Clock()

    s          = make_state()
    mode_idx   = 0
    prev_pos   = {1: None, 3: None}   # per button last position

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == QUIT:
                running = False
            elif event.type == KEYDOWN:
                if event.key == K_ESCAPE:
                    running = False
                elif event.key == K_r:
                    s = make_state()
                elif event.key == K_c:
                    mode_idx = (mode_idx + 1) % len(COLOUR_MODES)

        buttons = pygame.mouse.get_pressed()
        mx, my  = pygame.mouse.get_pos()
        gx = max(1, min(N, mx // SCALE + 1))
        gy = max(1, min(N, my // SCALE + 1))

        for btn, dye_key in [(1, "den"), (3, "den2")]:
            btn_idx = 0 if btn == 1 else 2
            if buttons[btn_idx]:
                s[dye_key][gx, gy] += 6.0
                if prev_pos[btn]:
                    pgx = max(1, min(N, prev_pos[btn][0] // SCALE + 1))
                    pgy = max(1, min(N, prev_pos[btn][1] // SCALE + 1))
                    s["vx"][gx, gy] += (gx - pgx) * 4.0
                    s["vy"][gx, gy] += (gy - pgy) * 4.0
                prev_pos[btn] = (mx, my)
            else:
                prev_pos[btn] = None

        step(s)
        render(screen, s, COLOUR_MODES[mode_idx])

        fps_surf = clock.render(
            f"FPS {ticker.get_fps():.0f}  |  theme: {COLOUR_MODES[mode_idx]}",
            True, (220, 220, 220)
        )
        screen.blit(fps_surf, (8, 8))

        pygame.display.flip()
        ticker.tick(FPS)

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
