#!/bin/bash
set -e

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║     KubeHealer Demo — Cluster Setup   ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# ── Prerequisites ──────────────────────────────────────────
for cmd in kind kubectl docker; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "  [FAIL] $cmd not found. Please install it first."
        exit 1
    fi
    echo "  [OK] $cmd"
done
echo ""

# ── Kind cluster ───────────────────────────────────────────
echo "  Creating Kind cluster 'kubehealer'..."
kind delete cluster --name kubehealer 2>/dev/null || true
kind create cluster --name kubehealer --wait 60s
echo "  [OK] Cluster created"
echo ""

# ── Deploy broken apps ────────────────────────────────────
echo "  Deploying intentionally broken apps..."
kubectl apply -f chaos/
echo ""

echo "  Waiting 15s for pods to crash..."
sleep 15

echo ""
echo "  Pod status:"
echo "  ─────────────────────────────────────────"
kubectl get pods --no-headers | while read line; do
    echo "    $line"
done
echo ""

echo "  ================================================"
echo "  Cluster ready! Broken pods deployed."
echo ""
echo "  Next steps:"
echo "    Terminal 2:  temporal server start-dev"
echo "    Terminal 3:  python worker.py"
echo "    Terminal 4:  python cli.py"
echo ""
echo "  Temporal UI:   http://localhost:8233"
echo "  ================================================"
echo ""
