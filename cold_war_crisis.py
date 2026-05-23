#!/usr/bin/env python3
"""
Cold War Crisis — top-down survival shooter
Survive endless waves of Soviet soldiers!

Controls:
  WASD / Arrow keys : move
  Mouse             : aim
  Hold LMB          : shoot
  R                 : restart (after death)
  ESC               : quit

Requirements: pip3 install pygame
"""

import sys, math, random
import pygame

WIN_W, WIN_H = 960, 700
FPS = 60

# ── Palette ───────────────────────────────────────────────────────────────────
BG        = (28,  38,  28)
BG_DARK   = (22,  30,  22)
WHITE     = (255, 255, 255)
BLACK     = (0,   0,   0)
RED       = (220,  50,  50)
DKRED     = (140,  20,  20)
YELLOW    = (255, 220,  50)
GRAY      = (150, 150, 150)
ORANGE    = (255, 140,  30)

P_BODY    = (60,  120, 220)
P_DARK    = (30,   60, 150)
P_EYE     = (100, 200, 100)

E_JACKET  = (100,  88,  55)   # olive drab
E_JACKET2 = (70,   60,  35)
E_SKIN    = (210, 170, 120)
HAT_MAIN  = (65,   48,  30)   # dark brown ushanka
HAT_FUR   = (110,  88,  55)
STAR_RED  = (210,  25,  25)

HP_GREEN  = (60,  200,  80)
HP_RED    = (200,  60,  60)
BLOOD     = (160,   0,   0)
BLOOD2    = (100,   0,   0)
MUZZLE    = (255, 230, 100)


# ── Utility ───────────────────────────────────────────────────────────────────
def draw_star(surf, cx, cy, r, color):
    pts = []
    for i in range(5):
        ao = math.pi * (i * 2 / 5 - 0.5)
        ai = ao + math.pi / 5
        pts += [(cx + math.cos(ao)*r,       cy + math.sin(ao)*r),
                (cx + math.cos(ai)*r*0.38,  cy + math.sin(ai)*r*0.38)]
    pygame.draw.polygon(surf, color, pts)


def edge_spawn():
    side = random.randint(0, 3)
    if side == 0: return random.randint(0, WIN_W), -35
    if side == 1: return random.randint(0, WIN_W), WIN_H + 35
    if side == 2: return -35, random.randint(0, WIN_H)
    return WIN_W + 35, random.randint(0, WIN_H)


# ── Particle ──────────────────────────────────────────────────────────────────
class Particle:
    def __init__(self, x, y, vx, vy, color, life, r=3):
        self.x, self.y   = float(x), float(y)
        self.vx, self.vy = float(vx), float(vy)
        self.color       = color
        self.life        = float(life)
        self.max_life    = float(life)
        self.r           = r

    def update(self, dt):
        self.x  += self.vx * dt
        self.y  += self.vy * dt
        self.vy += 90 * dt
        self.vx *= (1 - dt * 2)
        self.life -= dt

    def draw(self, surf):
        t = max(0.0, self.life / self.max_life)
        rad = max(1, int(self.r * t))
        c = tuple(min(255, int(ch * (0.4 + 0.6*t))) for ch in self.color)
        pygame.draw.circle(surf, c, (int(self.x), int(self.y)), rad)


# ── Bullet ────────────────────────────────────────────────────────────────────
class Bullet:
    SPEED  = 600
    RADIUS = 5
    DAMAGE = 35

    def __init__(self, x, y, angle):
        self.x, self.y = float(x), float(y)
        self.vx = math.cos(angle) * self.SPEED
        self.vy = math.sin(angle) * self.SPEED
        self.alive = True

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt
        if not (-20 <= self.x <= WIN_W+20 and -20 <= self.y <= WIN_H+20):
            self.alive = False

    def draw(self, surf):
        cx, cy = int(self.x), int(self.y)
        # tracer trail
        tx = int(self.x - self.vx * 0.025)
        ty = int(self.y - self.vy * 0.025)
        pygame.draw.line(surf, ORANGE, (tx, ty), (cx, cy), 2)
        pygame.draw.circle(surf, YELLOW, (cx, cy), self.RADIUS)
        pygame.draw.circle(surf, WHITE,  (cx, cy), 2)


# ── Enemy ─────────────────────────────────────────────────────────────────────
class Enemy:
    RADIUS = 18
    CONTACT_DPS = 14   # damage per second when touching player

    def __init__(self, x, y, hp, speed):
        self.x, self.y = float(x), float(y)
        self.hp        = float(hp)
        self.max_hp    = float(hp)
        self.speed     = speed
        self.alive     = True
        self.facing    = 0.0
        self._bob      = random.uniform(0, math.tau)

    def update(self, dt, px, py):
        dx = px - self.x
        dy = py - self.y
        dist = math.hypot(dx, dy) or 1
        self.facing = math.atan2(dy, dx)
        self.x += dx / dist * self.speed * dt
        self.y += dy / dist * self.speed * dt
        self._bob += dt * 6

    def draw(self, surf):
        cx, cy = int(self.x), int(self.y)
        fa     = self.facing
        bob    = math.sin(self._bob) * 1.5

        # Shadow
        pygame.draw.ellipse(surf, (15, 22, 15),
                            (cx-14, cy+14+int(bob), 28, 8))

        # Body / jacket
        pygame.draw.circle(surf, E_JACKET,  (cx, cy+2+int(bob)), 17)
        pygame.draw.circle(surf, E_JACKET2, (cx, cy+2+int(bob)), 17, 2)

        # Head offset in facing direction
        hx = cx + int(math.cos(fa) * 5)
        hy = cy + int(math.sin(fa) * 5) - 10 + int(bob)

        # Neck
        pygame.draw.line(surf, E_SKIN, (cx, cy-4+int(bob)), (hx, hy+8), 6)

        # Head
        pygame.draw.circle(surf, E_SKIN, (hx, hy), 11)

        # Ushanka hat body
        hat_pts = [(hx-12, hy-4), (hx+12, hy-4),
                   (hx+10, hy-14), (hx-10, hy-14)]
        pygame.draw.polygon(surf, HAT_MAIN, hat_pts)
        # Fur band
        pygame.draw.rect(surf, HAT_FUR, (hx-13, hy-7, 26, 6), border_radius=3)
        # Ear flaps
        pygame.draw.rect(surf, HAT_FUR, (hx-16, hy-5, 5, 9), border_radius=2)
        pygame.draw.rect(surf, HAT_FUR, (hx+11, hy-5, 5, 9), border_radius=2)
        # Red star on hat
        draw_star(surf, hx, hy-10, 5, STAR_RED)

        # Gun arm
        gx = cx + int(math.cos(fa) * 28)
        gy = cy + int(math.sin(fa) * 28) + int(bob)
        pygame.draw.line(surf, (50, 50, 50), (cx, cy+int(bob)), (gx, gy), 5)
        pygame.draw.circle(surf, (70, 70, 70), (gx, gy), 3)

        # HP bar
        bw = 36
        bx, by = cx - bw//2, cy - self.RADIUS - 12
        pygame.draw.rect(surf, HP_RED,   (bx, by, bw, 5))
        fill = int(bw * max(0, self.hp / self.max_hp))
        pygame.draw.rect(surf, HP_GREEN, (bx, by, fill, 5))
        pygame.draw.rect(surf, (200, 200, 200), (bx, by, bw, 5), 1)

    def hit(self, dmg):
        self.hp -= dmg
        if self.hp <= 0:
            self.alive = False


# ── Player ────────────────────────────────────────────────────────────────────
class Player:
    RADIUS    = 18
    SPEED     = 240
    MAX_HP    = 100
    FIRE_RATE = 0.14

    def __init__(self):
        self.reset()

    def reset(self):
        self.x     = float(WIN_W // 2)
        self.y     = float(WIN_H // 2)
        self.hp    = float(self.MAX_HP)
        self.alive = True
        self._cd   = 0.0
        self.angle = 0.0
        self.score = 0
        self._step = 0.0

    def update(self, dt, keys, mx, my, enemies):
        if not self.alive:
            return
        dx = dy = 0
        if keys[pygame.K_w] or keys[pygame.K_UP]:    dy -= 1
        if keys[pygame.K_s] or keys[pygame.K_DOWN]:  dy += 1
        if keys[pygame.K_a] or keys[pygame.K_LEFT]:  dx -= 1
        if keys[pygame.K_d] or keys[pygame.K_RIGHT]: dx += 1
        moving = bool(dx or dy)
        if moving:
            mag = math.hypot(dx, dy)
            self.x = max(self.RADIUS, min(WIN_W-self.RADIUS, self.x + dx/mag * self.SPEED * dt))
            self.y = max(self.RADIUS, min(WIN_H-self.RADIUS, self.y + dy/mag * self.SPEED * dt))
            self._step += dt * 10
        self.angle = math.atan2(my - self.y, mx - self.x)
        self._cd   = max(0.0, self._cd - dt)

        prev_hp = self.hp
        for e in enemies:
            if e.alive and math.hypot(e.x-self.x, e.y-self.y) < self.RADIUS + e.RADIUS:
                self.hp -= e.CONTACT_DPS * dt
        damaged = self.hp < prev_hp
        if self.hp <= 0:
            self.hp    = 0
            self.alive = False
        return damaged

    def try_shoot(self, bullets):
        if self._cd <= 0:
            bullets.append(Bullet(self.x, self.y, self.angle))
            self._cd = self.FIRE_RATE
            return True
        return False

    def draw(self, surf):
        cx, cy = int(self.x), int(self.y)
        r      = self.RADIUS
        bob    = math.sin(self._step) * 1.8

        # Shadow
        pygame.draw.ellipse(surf, (15, 22, 15), (cx-14, cy+14, 28, 8))

        # Legs (simple rects when moving)
        leg_off = int(math.sin(self._step) * 5)
        pygame.draw.rect(surf, (30, 50, 120),
                         (cx-6, cy+8+leg_off, 5, 10), border_radius=2)
        pygame.draw.rect(surf, (30, 50, 120),
                         (cx+1, cy+8-leg_off, 5, 10), border_radius=2)

        # Body
        pygame.draw.circle(surf, P_BODY, (cx, int(cy+bob)), r)
        pygame.draw.circle(surf, P_DARK, (cx, int(cy+bob)), r, 2)

        # Gun barrel
        gx = cx + int(math.cos(self.angle) * (r+14))
        gy = int(cy+bob) + int(math.sin(self.angle) * (r+14))
        pygame.draw.line(surf, (180,180,180), (cx, int(cy+bob)), (gx, gy), 6)
        pygame.draw.line(surf, WHITE,         (cx, int(cy+bob)), (gx, gy), 2)

        # Helmet visor
        ex = cx + int(math.cos(self.angle) * 9)
        ey = int(cy+bob) + int(math.sin(self.angle) * 9)
        pygame.draw.circle(surf, (30, 180, 255), (ex, ey), 6)
        pygame.draw.circle(surf, WHITE, (ex, ey), 6, 1)

        # HP bar
        bw = 44
        bx, by = cx - bw//2, cy - r - 14
        pygame.draw.rect(surf, HP_RED,   (bx, by, bw, 7))
        fill = int(bw * max(0, self.hp / self.MAX_HP))
        pygame.draw.rect(surf, HP_GREEN, (bx, by, fill, 7))
        pygame.draw.rect(surf, WHITE, (bx, by, bw, 7), 1)


# ── Game ──────────────────────────────────────────────────────────────────────
class Game:
    WAVE_PAUSE = 3.5

    def __init__(self, screen):
        self.screen   = screen
        self.font_big = pygame.font.SysFont("impact", 56)
        self.font_med = pygame.font.SysFont("impact", 28)
        self.font_sm  = pygame.font.SysFont("monospace", 16)
        self._init()

    def _init(self):
        self.player    = Player()
        self.bullets   = []
        self.enemies   = []
        self.particles = []
        self.wave      = 0
        self.pausing   = False
        self.pause_t   = 0.0
        self.high      = getattr(self, 'high', 0)
        self._flash    = 0.0        # red damage flash intensity
        self._bg_surf  = self._make_bg()
        self._next_wave()

    def _make_bg(self):
        s = pygame.Surface((WIN_W, WIN_H))
        for tx in range(0, WIN_W, 48):
            for ty in range(0, WIN_H, 48):
                col = BG_DARK if (tx//48 + ty//48) % 2 == 0 else BG
                pygame.draw.rect(s, col, (tx, ty, 48, 48))
                # subtle cross
                pygame.draw.line(s, (33,44,33), (tx+24, ty), (tx+24, ty+48))
                pygame.draw.line(s, (33,44,33), (tx, ty+24), (tx+48, ty+24))
        return s

    def _next_wave(self):
        self.wave += 1
        count = 4 + self.wave * 3
        hp    = 45 + self.wave * 18
        spd   = 75 + self.wave * 9
        for _ in range(count):
            x, y = edge_spawn()
            self.enemies.append(Enemy(x, y, hp, spd))
        self.pausing = False

    def _burst(self, x, y, count, color1, color2=None):
        for _ in range(count):
            a  = random.uniform(0, math.tau)
            sp = random.uniform(50, 220)
            lf = random.uniform(0.25, 0.9)
            col = color1 if (color2 is None or random.random() < 0.6) else color2
            r = random.randint(2, 5)
            self.particles.append(Particle(
                x, y, math.cos(a)*sp, math.sin(a)*sp, col, lf, r))

    def reset(self):
        self.high = max(self.high, self.player.score)
        self._init()

    def update(self, dt, keys, mx, my, shooting):
        p = self.player
        damaged = p.update(dt, keys, mx, my, self.enemies)
        if damaged:
            self._flash = min(1.0, self._flash + 0.4)

        if shooting and p.alive:
            if p.try_shoot(self.bullets):
                gx = p.x + math.cos(p.angle)*(p.RADIUS+16)
                gy = p.y + math.sin(p.angle)*(p.RADIUS+16)
                self._burst(gx, gy, 4, MUZZLE, WHITE)

        for b in self.bullets:
            b.update(dt)

        # Bullet-enemy hits
        for b in self.bullets:
            if not b.alive:
                continue
            for e in self.enemies:
                if not e.alive:
                    continue
                if math.hypot(b.x-e.x, b.y-e.y) < b.RADIUS + e.RADIUS:
                    e.hit(b.DAMAGE)
                    b.alive = False
                    self._burst(b.x, b.y, 10, BLOOD, BLOOD2)
                    if not e.alive:
                        p.score += 10 * self.wave
                        self._burst(e.x, e.y, 25, BLOOD, BLOOD2)
                    break

        for e in self.enemies:
            if e.alive:
                e.update(dt, p.x, p.y)

        for pt in self.particles:
            pt.update(dt)

        self.bullets   = [b  for b  in self.bullets   if b.alive]
        self.enemies   = [e  for e  in self.enemies   if e.alive]
        self.particles = [pt for pt in self.particles if pt.life > 0]

        self._flash = max(0.0, self._flash - dt * 4)

        # Wave transition
        if not self.enemies and not self.pausing and p.alive:
            self.pausing = True
            self.pause_t = self.WAVE_PAUSE

        if self.pausing:
            self.pause_t -= dt
            if self.pause_t <= 0:
                self._next_wave()

    def draw(self, mx, my):
        screen = self.screen

        # Background
        screen.blit(self._bg_surf, (0, 0))

        # Particles (behind everything)
        for pt in self.particles:
            pt.draw(screen)

        # Enemies
        for e in self.enemies:
            e.draw(screen)

        # Bullets
        for b in self.bullets:
            b.draw(screen)

        # Player
        if self.player.alive:
            self.player.draw(screen)

        # Crosshair
        self._draw_crosshair(screen, mx, my)

        # Damage flash
        if self._flash > 0:
            ov = pygame.Surface((WIN_W, WIN_H), pygame.SRCALPHA)
            ov.fill((200, 0, 0, int(self._flash * 90)))
            screen.blit(ov, (0, 0))

        # HUD
        self._hud(screen)

        # Overlays
        if not self.player.alive:
            self._overlay_death(screen)
        elif self.pausing:
            self._overlay_wave(screen)

    def _draw_crosshair(self, surf, mx, my):
        col  = (255, 80, 80)
        size = 14
        gap  = 5
        pygame.draw.line(surf, col, (mx-size, my), (mx-gap, my), 2)
        pygame.draw.line(surf, col, (mx+gap,  my), (mx+size, my), 2)
        pygame.draw.line(surf, col, (mx, my-size), (mx, my-gap), 2)
        pygame.draw.line(surf, col, (mx, my+gap),  (mx, my+size), 2)
        pygame.draw.circle(surf, col, (mx, my), 3, 1)

    def _hud(self, surf):
        p = self.player
        # Wave / score
        t1 = self.font_med.render(f"WAVE {self.wave}", True, YELLOW)
        t2 = self.font_med.render(f"SCORE  {p.score}", True, WHITE)
        t3 = self.font_sm.render(f"BEST {self.high}", True, GRAY)
        surf.blit(t1, (10, 8))
        surf.blit(t2, (10, 36))
        surf.blit(t3, (10, 64))

        # Enemies remaining
        rem = self.font_sm.render(f"Enemies remaining: {len(self.enemies)}", True, (200, 160, 160))
        surf.blit(rem, (WIN_W - rem.get_width() - 10, 10))

        # Health bar (bottom)
        bw, bh = 220, 16
        bx, by = WIN_W//2 - bw//2, WIN_H - 28
        pygame.draw.rect(surf, (40, 0, 0),   (bx-2, by-2, bw+4, bh+4), border_radius=4)
        pygame.draw.rect(surf, HP_RED,        (bx, by, bw, bh), border_radius=3)
        fill = int(bw * max(0, p.hp / p.MAX_HP))
        pygame.draw.rect(surf, HP_GREEN,      (bx, by, fill, bh), border_radius=3)
        pygame.draw.rect(surf, WHITE,         (bx, by, bw, bh), 1, border_radius=3)
        ht = self.font_sm.render(f"HP  {int(p.hp)}", True, WHITE)
        surf.blit(ht, (WIN_W//2 - ht.get_width()//2, by + 1))

    def _overlay_death(self, surf):
        ov = pygame.Surface((WIN_W, WIN_H), pygame.SRCALPHA)
        ov.fill((0, 0, 0, 170))
        surf.blit(ov, (0, 0))

        t1 = self.font_big.render("YOU HAVE FALLEN", True, RED)
        t2 = self.font_med.render(f"Wave {self.wave}  —  Score {self.player.score}", True, WHITE)
        t3 = self.font_med.render(f"Best: {self.high}", True, YELLOW)
        t4 = self.font_sm.render("[ R ] Play Again     [ ESC ] Quit", True, GRAY)

        cy = WIN_H // 2
        surf.blit(t1, (WIN_W//2 - t1.get_width()//2, cy - 90))
        surf.blit(t2, (WIN_W//2 - t2.get_width()//2, cy - 20))
        surf.blit(t3, (WIN_W//2 - t3.get_width()//2, cy + 18))
        surf.blit(t4, (WIN_W//2 - t4.get_width()//2, cy + 64))

    def _overlay_wave(self, surf):
        alpha = int(min(1.0, self.pause_t / 1.0) * 220)
        t1 = self.font_big.render(f"WAVE {self.wave - 1}  CLEARED!", True, YELLOW)
        t2 = self.font_med.render(f"Next wave in  {max(0.0, self.pause_t):.1f}s", True, WHITE)

        ov = pygame.Surface((WIN_W, 110), pygame.SRCALPHA)
        ov.fill((0, 0, 0, 150))
        surf.blit(ov, (0, WIN_H//2 - 55))

        t1.set_alpha(alpha); t2.set_alpha(alpha)
        surf.blit(t1, (WIN_W//2 - t1.get_width()//2, WIN_H//2 - 45))
        surf.blit(t2, (WIN_W//2 - t2.get_width()//2, WIN_H//2 + 10))


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption("Cold War Crisis — Survive the Soviet Assault!")
    clock = pygame.time.Clock()

    game     = Game(screen)
    shooting = False

    while True:
        dt = min(clock.tick(FPS) / 1000.0, 0.05)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit(); sys.exit()
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    pygame.quit(); sys.exit()
                if event.key == pygame.K_r and not game.player.alive:
                    game.reset()
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                shooting = True
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                shooting = False

        keys   = pygame.key.get_pressed()
        mx, my = pygame.mouse.get_pos()

        game.update(dt, keys, mx, my, shooting)
        game.draw(mx, my)
        pygame.display.flip()


if __name__ == "__main__":
    main()
