# End-to-end cross-view vehicle localization on satellite imagery using Geometry Guided Kernel Transformer

# Abstract
In this project, we developed a cross-view end-to-end localization pipeline using both ground-view and satellite images. Existing methods use CNN-based feature extractors to learn a robust representation to bridge the cross-view domain gap. We proposed a simpler but more powerful approach by utilizing previous works on BEV generation. A transformer model is used to generate BEV features from six ground-view cameras. We then apply a similar backbone network to generate features from satellite maps. Levenberg-Marquardt (LM) optimization is further utilized to fine-tune the transformation between the BEV features and satellite features. Our experiments have shown that with rich and patterned features, LM optimization is able to estimate the transformation between two features. In our end-to-end training pipeline, we observed features from two domains can converge in terms of their edges and patterns. However, the localization module is not working properly due to the limited pose estimation quality. Our code can be found here: We collected the satellite images for the nuScences datasets and will open this appended dataset to future researchers.

![](https://i.imgur.com/PlXORW2.png)
### Docker command
To run the code in this repository, we recommend using docker to guarantee the same environment. Detailed instruction could be found here:
1. Enter the docker folder `cd docker`
2. Build docker image: `docker build --tag satellite_slam/pytorch_env .`
3. Build docker environment: `chmod +x build_docker_container.sh && ./build_docker_container.sh satellite_slam`. Please enter `exit` once entering the container, we will rerun it below to be consistent.
4. Start existing docker: `docker start satellite_slam`
5. Execute the container: `docker exec -it -u root satellite_slam bash` 
6. You should be albe to develop inside the container

### Run localization using nuScenes dataset:
So far we have two options to run: (1) single-level LM (`single-level/transformer` branch), (2) multi-level LM (`exp/transformer` branch). You can choose the desire branch to run experiment on.

`python train_nuscenes.py`: This will kick of a training pipeline.


### Experiment Dataset
We use nuScenes dataset to run experiment. Nuscenes dataset can be downloaded from [here](https://www.nuscenes.org/nuscenes#download). For the corresponding satellite dataset, please email `leekt@umich.edu` or `goroyeh.umich.edu`, we will then send you the link for download.

### Publications
To be continued
