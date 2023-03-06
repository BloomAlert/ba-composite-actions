# BloomAlert Composite Actions 

This repo contains the following structure

├── test-python             # Steps to Lint with flake8, test with pytest and upload coverage to Codecov
│   ├── action.yml          
├── build-podman            # Build with Podman (to generate docker v2 image format)
│   ├── action.yml          
├── build-docker            # Build with docker buildx (with cache) 
│   ├── action.yml         
├── deploy-cloudrun         # Deploy to Cloud Run 
│   ├── action.yml          
