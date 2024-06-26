import os
import numpy as np
import torch
import argparse
import traceback
import cv2
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import keras_ocr
import easyocr
import csv

from utils.data_processing import process_image
from utils.text_utils import labels_to_text
from model import TransformerModel
from config import Hparams


def draw_annotations(image, bboxes, labels):
    """
    Draw bounding boxes and labels on the image.

    Args:
        image (np.array): The image to draw annotations on.
        bboxes (list): List of bounding boxes.
        labels (list): List of labels corresponding to the bounding boxes.

    Returns:
        fig: A Matplotlib figure with the annotations drawn.
    """
    # Convert bounding boxes to the correct format
    bboxes = [np.array(bbox).astype('float32') for bbox in bboxes]

    # Create a list of predictions where each prediction is a tuple of a word and its box
    predictions = list(zip(labels, bboxes))

    # Use Keras OCR's drawAnnotations function to draw the predictions
    fig = keras_ocr.tools.drawAnnotations(image=image, predictions=predictions)

    return fig


def inference(model, image_input, char2idx, idx2char, hp):
    """
    Perform inference on a given image using a trained model.

    Args:
        model (nn.Module): The trained model.
        image_input (np.array): The input image.
        char2idx (dict): A dictionary mapping characters to indices.
        idx2char (dict): A dictionary mapping indices to characters.
        hp: Hyperparameters (config).

    Returns:
        predicted_transcript (str): The transcript predicted by the model.
    """
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    img = process_image(image_input, hp).astype('uint8')
    img = img / img.max()
    img = np.transpose(img, (2, 0, 1))
    src = torch.FloatTensor(img).unsqueeze(0).to(device)
    out_indexes = [char2idx['SOS'], ]
    for i in range(100):
        trg_tensor = torch.LongTensor(out_indexes).unsqueeze(1).to(device)
        output = model(src, trg_tensor)
        out_token = output.argmax(2)[-1].item()
        out_indexes.append(out_token)
        if out_token == char2idx['EOS']:
            break
    predicted_transcript = labels_to_text(out_indexes[1:], idx2char)
    return predicted_transcript


def load_image(image_path):
    """
    Load an image from a file.

    Args:
        image_path (str): The path to the image file.

    Returns:
        image (np.array): The loaded image.
    """
    image = cv2.imread(image_path)
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    return image


def draw_bboxes(image, detected_bboxes, model, char2idx, idx2char, hp):
    """
    Draw bounding boxes on an image and perform inference on each bounding box.

    Args:
        image (np.array): The image to draw bounding boxes on.
        detected_bboxes (list): List of detected bounding boxes.
        model (nn.Module): The trained model.
        char2idx (dict): A dictionary mapping characters to indices.
        idx2char (dict): A dictionary mapping indices to characters.

    Returns:
        image_copy (np.array): The image with bounding boxes drawn on it.
        all_bboxes (list): List of all bounding boxes.
        predicted_transcripts (list): List of transcripts predicted for each bounding box.
    """
    image_copy = image.copy()
    all_bboxes = []
    predicted_transcripts = []

    for detected_bbox in detected_bboxes:
        bbox, label, score = detected_bbox
        # Convert the bounding box to the format used by Keras OCR
        pts = [tuple(map(int, pt)) for pt in bbox]
        all_bboxes.append(pts)

        pts = np.array(pts, np.int32)
        pts = pts.reshape((-1,1,2))
        cv2.polylines(image_copy, [pts], True, (0,255,0), 3)

        x_min, y_min = np.min(pts, axis=0)[0]
        x_max, y_max = np.max(pts, axis=0)[0]
        extracted_image = image[y_min:y_max, x_min:x_max]

        # Check if the cropped image is not zero-sized
        if extracted_image.size > 0:
            predicted_transcript = inference(model, extracted_image, char2idx, idx2char, hp)
            predicted_transcripts.append(predicted_transcript)
            # print(f"Predicted transcript: {predicted_transcript}")
        else:
            print("Warning: Cropped image has zero size, skipping processing.")

    return image_copy, all_bboxes, predicted_transcripts


def main():
    parser = argparse.ArgumentParser(description='OCR Inference')
    parser.add_argument("--config", default="configs/config.json", help="Path to JSON configuration file")
    parser.add_argument('--weights', type=str, default='ocr_transformer_rn50_64x256_53str_jit.pt', help='Path to the weights file')
    parser.add_argument('--input_dir', type=str, default='demo/input', help='Directory of input images')
    parser.add_argument('--output_dir', type=str, default='demo/output', help='File to output the results')
    parser.add_argument('--image_file', type=str, help='Specific image file to process')
    parser.add_argument('--dump_bboxes', type=bool, default=False, help='Whether to dump bounding box results')
    parser.add_argument('--dump_ocr', type=bool, default=False, help='Whether to dump OCR results')
    parser.add_argument('--dump_dir', type=str, default='demo/dump', help='Directory to dump results')

    args = parser.parse_args()

    args.input_dir = os.path.abspath(args.input_dir)
    args.output_dir = os.path.abspath(args.output_dir)
    args.weights = os.path.abspath(args.weights)
    args.dump_dir = os.path.abspath(args.dump_dir)

    if args.image_file:
        args.input_dir = os.path.dirname(os.path.abspath(args.image_file))

    hp = Hparams(args.config)
    print(vars(hp))

    reader = easyocr.Reader(['ru'])

    print("Weights file path:", os.path.abspath(args.weights))

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    model = TransformerModel('resnet50', len(hp.cyrillic), hidden=hp.hidden, enc_layers=hp.enc_layers, dec_layers=hp.dec_layers,
                             nhead=hp.nhead, dropout=hp.dropout).to(device)
    model.load_state_dict(torch.load(args.weights, map_location=device)['model'])

    char2idx = {char: idx for idx, char in enumerate(hp.cyrillic)}
    idx2char = {idx: char for idx, char in enumerate(hp.cyrillic)}

    # Perform inference on each image in the input directory
    if args.image_file:
        image_files = [os.path.basename(args.image_file)]
    else:
        image_files = os.listdir(args.input_dir)

    for image_path in image_files:
        # Check if the file is an image
        if image_path.lower().endswith(('.png', '.jpg', '.jpeg')):
            try:
                image_input = os.path.join(args.input_dir, image_path)
                print(f"Processing image: {image_input}")

                img = mpimg.imread(image_input)

                print(f"Processing img: {img.shape}")

                detected_bboxes = reader.readtext(image_input)
                detected_bboxes.sort(
                    key=lambda bbox: (np.mean([pt[1] for pt in bbox[0]]), np.mean([pt[0] for pt in bbox[0]])))

                image_with_bboxes, all_bboxes, predicted_transcripts = draw_bboxes(img, detected_bboxes, model,
                                                                                   char2idx, idx2char, hp)

                # Save the figure with the predicted bounding box
                plt.figure(figsize=(10, 10))
                plt.imshow(image_with_bboxes)
                plt.savefig(os.path.join(args.output_dir, f"{os.path.splitext(image_path)[0]}_bbox.png"))

                # Perform inference on each bounding box and save the results
                results = []
                for i, detected_bbox in enumerate(detected_bboxes):
                    bbox, label, score = detected_bbox
                    bbox = [[int(coordinate) for coordinate in point] for point in
                            bbox]  # Convert coordinates to integers
                    cropped_img = img[bbox[0][1]:bbox[2][1], bbox[0][0]:bbox[2][0]]
                    if cropped_img.size > 0:
                        predicted_transcript = inference(model, cropped_img, char2idx, idx2char, hp)
                        print(f"Predicted transcript for bbox {i + 1}: {predicted_transcript}")
                        results.append([str(bbox), score, predicted_transcript])  # Convert bbox to string

                # Save all results to a single CSV file
                with open(os.path.join(args.dump_dir, f"{os.path.splitext(image_path)[0]}_all_bboxes.csv"), 'w',
                          newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow(["bbox_coords", "bbox_confidence", "predicted_labels"])
                    writer.writerows(results)

            except Exception as e:
                print(f"Error processing {image_path}: {e}")
                traceback.print_exc()


if __name__ == "__main__":
    main()
