import numpy as np

# ── A simple 2D loss function: narrow valley
# L(w) = 0.5*w0^2 + 25*0.5*w1^2
# Gradient: [w0, 25*w1]
def loss(w):
    return 0.5 * w[0]**2 + 25 * 0.5 * w[1]**2

def grad(w):
    return np.array([w[0], 25 * w[1]])


# ── RMSprop ────────────────────────────────────────────────────────────────
def rmsprop(w_init, eta=0.1, beta=0.9, eps=1e-8, n_steps=50):
    w = w_init.copy()
    s = np.zeros_like(w)          # running average of squared gradients, one per param

    history = [w.copy()]

    for t in range(n_steps):
        g = grad(w)

        # Step 1: update running average of squared gradient (eq. 11)
        s = beta * s + (1 - beta) * g**2

        # Step 2: update weights, normalising lr by sqrt of running average (eq. 12)
        w = w - eta * g / (np.sqrt(s) + eps)

        history.append(w.copy())

    return np.array(history)


# ── Vanilla SGD (for comparison) ───────────────────────────────────────────
def sgd(w_init, eta=0.1, n_steps=50):
    w = w_init.copy()
    history = [w.copy()]

    for t in range(n_steps):
        g = grad(w)
        w = w - eta * g
        history.append(w.copy())

    return np.array(history)


# ── Run both ───────────────────────────────────────────────────────────────
w0 = np.array([3.0, 0.3])

rms_path = rmsprop(w0, eta=0.1, beta=0.9)
sgd_path = sgd(w0,    eta=0.02)   # SGD needs much smaller lr to not diverge on steep axis

print("Step | Loss (RMSprop) | Loss (SGD)")
print("-----|----------------|----------")
for i in [0, 5, 10, 20, 50]:
    print(f"  {i:2d} |   {loss(rms_path[i]):.6f}   |  {loss(sgd_path[i]):.6f}")

print(f"\nRMSprop final w: {rms_path[-1]}")
print(f"SGD     final w: {sgd_path[-1]}")