"""Fine-tune YOLOv8n (COCO-pretrained) on a public single-class drone dataset, validate, export ONNX for the Pi 4.
Dataset: HF kc34251/Drone-Detection (YOLO-format mirror of Kaggle 'drone-dataset-uav', dasmehdixtr).
Usage: python scripts/train_vision.py <dataset_txt dir with N.jpg + N.txt> [--epochs 12] [--imgsz 416]
Writes models/drone_n.pt, models/drone_n.onnx, results/vision_eval.json"""
import argparse, json, random, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("src"); ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--imgsz", type=int, default=416); ap.add_argument("--work", default=str(ROOT.parent / "data" / "yolo_split"))
    a = ap.parse_args()
    src, work = Path(a.src), Path(a.work)
    imgs = sorted(p for p in src.glob("*.jpg") if (src / (p.stem + ".txt")).exists())
    random.Random(0).shuffle(imgs)
    n_val = int(len(imgs) * 0.15)
    for split, items in (("val", imgs[:n_val]), ("train", imgs[n_val:])):
        for sub in ("images", "labels"):
            (work / sub / split).mkdir(parents=True, exist_ok=True)
        for p in items:
            shutil.copy(p, work / "images" / split / p.name); shutil.copy(src / (p.stem + ".txt"), work / "labels" / split / (p.stem + ".txt"))
    (work / "data.yaml").write_text(f"path: {work.as_posix()}\ntrain: images/train\nval: images/val\nnames:\n  0: drone\n")
    print(f"{len(imgs)-n_val} train / {n_val} val images")
    from ultralytics import YOLO
    m = YOLO("yolov8n.pt")
    m.train(data=str(work / "data.yaml"), epochs=a.epochs, imgsz=a.imgsz, batch=16, device="cpu", workers=4,
            project=str(ROOT / "runs"), name="drone_n", exist_ok=True, patience=6, verbose=False, plots=False)
    best = YOLO(str(ROOT / "runs" / "drone_n" / "weights" / "best.pt"))
    v = best.val(data=str(work / "data.yaml"), imgsz=a.imgsz, device="cpu", plots=False, verbose=False)
    res = {"train_images": len(imgs) - n_val, "val_images": n_val, "epochs": a.epochs, "imgsz": a.imgsz,
           "precision": round(float(v.box.mp), 4), "recall": round(float(v.box.mr), 4), "mAP50": round(float(v.box.map50), 4), "mAP50-95": round(float(v.box.map), 4)}
    print(res)
    (ROOT / "models").mkdir(exist_ok=True); (ROOT / "results").mkdir(exist_ok=True)
    shutil.copy(ROOT / "runs" / "drone_n" / "weights" / "best.pt", ROOT / "models" / "drone_n.pt")
    onnx = best.export(format="onnx", imgsz=a.imgsz, simplify=True)
    shutil.copy(onnx, ROOT / "models" / "drone_n.onnx")
    (ROOT / "results" / "vision_eval.json").write_text(json.dumps(res, indent=1))
