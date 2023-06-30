'''
@File    :   dilate_overlap.py
@Time    :   2023/06/28 11:19:09
@Author  :   Surya Chandra Kalia
@Version :   1.0
@Contact :   suryackalia@gmail.com
@Org     :   Kasthuri Lab, University of Chicago
'''

import h5py
import numpy as np
from sk_connectomics.util.coordinate import *
import csv  
import os
import lzma
import pickle
from scipy import ndimage
import glob


# Given a segmentation map (3D), we will dilate the neuron boundaries and identify overlap between neuron pairs.
# Step-by-step control flow:
# 1. Read the dense segmentation map into memory. Each neuron is identified with a unique ID.
# 2. For each neuron ID, identify a bounding box capturing the min and max x,y,z coordinates.
# 3. Include padding and crop out each neuron segment as separate images. Keep a track of origin offset for each ID in a consolidated txt file as well.
#  This will be useful for reconstructing the segments back to global coordinates.
# 4. Dilate each segment by provided voxel amount.
# 5. Eliminate glea cells (having very large voxel counts) and background segments (having very small voxel counts)
# 6. Run n*n loop over all neuron pairs to identify overlapping regions. Most can be trivially eliminated by seeing bounding box overlap. 
#  The others will have to be idetified by croppnig out the bounding box intersection, performing a logical AND op, and storing the region of overlap

class DilateOverlap:
  def __init__(self, cremi_file_path, output_dir, dilation_voxel_count, voxel_volume_threshold):
    self.cremi_file_path = cremi_file_path
    self.output_dir = output_dir
    self.hdf_handle = h5py.File(cremi_file_path, 'r')
    self.neuron_ids = np.array(self.hdf_handle["volumes/labels/neuron_ids"])
    # # DEBUG: Use thin slice of input
    # self.neuron_ids = self.neuron_ids[:5, :, :]
    self.segment_black_list = []
    self.segment_volume_map = {}
    self.dilation_voxel_count = dilation_voxel_count
    self.voxel_volume_threshold = voxel_volume_threshold
  
  def create_bounding_boxes(self):
    # Scan through whole image and keep track of min/max x, y, z values of each segmentID
    self.minCoordMap = {}
    self.maxCoordMap = {}
    dims = self.neuron_ids.shape
    for z in range (dims[0]):
      print("Z layer num:", z)
      for y in range (dims[1]):
        for x in range (dims[2]):
          id = self.neuron_ids[z][y][x]
          if (id not in self.minCoordMap):
            # New ID encountered
            self.minCoordMap[id] = Coordinate(x, y, z)
            self.maxCoordMap[id] = Coordinate(x, y, z)
          else:
            # Update the min/max values for the ID
            self.minCoordMap[id] = minCoord(self.minCoordMap[id], Coordinate(x, y, z))
            self.maxCoordMap[id] = maxCoord(self.maxCoordMap[id], Coordinate(x, y, z))
  
  # Add list of segment IDs which should be treated as background voxels
  def blacklist_append(self, segment_list):
    self.segment_black_list.extend(segment_list)
  
  # Clear segment ID blacklist 
  def blacklist_clear(self):
    self.segment_black_list = []
  
  # Eliminate all segments smaller than voxel_threshold and previously blacklisted segment IDs
  def trim_invalid_segments(self):
    print("Trimming invalid segments")
    # Create a segmentId to volume map
    (unique, counts) = np.unique(self.neuron_ids, return_counts=True)
    for i in range (len(unique)):
      self.segment_volume_map[unique[i]] = counts[i]
    
    # To simplify the process, set blacklisted segments to have -1 voxel count. Will be eliminated by the voxel threshold
    for segmentID in self.segment_black_list:
      self.segment_volume_map[segmentID] = -1
      
    # Eliminate segments from input image with volume lesser than voxel_volume_threshold. Zeroing out all these segments in the raw input will be computationally expensive as we will have 
    # to incur a lookup cost for each voxel when iterating, and also modifying the data. Instead, we will jult eliminate the corresponding bounding boxes
    for segmentID in list(self.segment_volume_map.keys()) : 
      if (self.segment_volume_map[segmentID] < self.voxel_volume_threshold):
        del self.segment_volume_map[segmentID] 
        del self.minCoordMap[segmentID]
        del self.maxCoordMap[segmentID]
      
  # Given a list of trimmed bounding boxes, crop out the 3D segment masks and store as separate images, along with metadata 
  def crop_out_bounding_boxes(self):
    print("Cropping out bounding boxes")
    if not os.path.exists(self.output_dir):
      os.makedirs(self.output_dir)

    if not os.path.exists(self.output_dir+"/crops"):
      os.makedirs(self.output_dir+"/crops")
      
    # Clear out directory to avoid mixing up with old crops
    files = glob.glob(self.output_dir+"/crops/*")
    for f in files:
        os.remove(f)
    
    # Reverse sort by segment volume. n*n loop for img overlap will be more efficient if larger images are loaded in memory less often
    sorted_segment_volume_map = dict(sorted(self.segment_volume_map.items(), key = lambda x:x[1], reverse = True))
    
    crop_num = 1
    header = ['SEGMENT_ID', 'VOLUME', 'MIN_X', 'MIN_Y', 'MIN_Z', 'MAX_X', 'MAX_Y', 'MAX_Z']
    with open(self.output_dir + "/metadata.csv", 'w', encoding='UTF8') as f:
      writer = csv.writer(f)
      # write the header
      writer.writerow(header)
      for segmentID, volume in sorted_segment_volume_map.items():
        minCoord = self.minCoordMap[segmentID]
        maxCoord = self.maxCoordMap[segmentID]
        # Ensure that we crop out a larger bounding box to account for new size post dilation.
        minCoord = dilateMin(minCoord, self.dilation_voxel_count, self.neuron_ids.shape)
        maxCoord = dilateMax(maxCoord, self.dilation_voxel_count, self.neuron_ids.shape)
        
        # Crop out bounding box and mask out all voxels not belonging to the given segment
        img_crop = self.neuron_ids[minCoord.z:maxCoord.z+1, minCoord.y:maxCoord.y+1, minCoord.x:maxCoord.x+1]
        img_crop = np.where(img_crop == segmentID , 1, 0).astype(np.uint8)
        
        # Dilate image_crop
        for _ in range(self.dilation_voxel_count):
          img_crop = ndimage.binary_dilation(img_crop).astype(img_crop.dtype)
        
        # Save cropped image mask. It is reduced to uint8 since we just have a bitmask at this stage
        with lzma.open(self.output_dir + "/crops/" + str(crop_num) + "_" + str(segmentID) + ".xz", "wb") as f:
          pickle.dump(img_crop, f)
        
        # Save metadata of img_crop for each segmentID
        data = [segmentID, volume, minCoord.x, minCoord.y, minCoord.z, maxCoord.x, maxCoord.y, maxCoord.z]
        writer.writerow(data)
        crop_num+=1

    print("Successfully created ", crop_num, " segment corps")



  def run(self):
    self.create_bounding_boxes()
    self.trim_invalid_segments()
    self.crop_out_bounding_boxes()




# References: 
# Scipy image dilaiton: https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.binary_dilation.html
# Compression methods comparison: https://stackoverflow.com/questions/57983431/whats-the-most-space-efficient-way-to-compress-serialized-python-data

