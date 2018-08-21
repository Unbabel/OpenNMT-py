SOURCE=de
TARGET=en
LANGPAIR=${SOURCE}-${TARGET}
DATA=/mnt/data/${LANGPAIR}-shr-big
ONMT=/home/ubuntu/OpenNMT-py-un
MODEL_FOLDER=base
MODEL_NAME=${LANGPAIR}-shr-big

python -u ${ONMT}/train.py \
	   -data ${DATA}/preprocessed-big-shr \
	   -save_model ${DATA}/${MODEL_FOLDER}/${MODEL_NAME} \
	   -layers 2 \
	   -encoder_type brnn \
	   -dropout 0.3 \
       -share_decoder_embeddings \
	   -epochs 15 \
	   -seed 42 \
       -gpu 0 \
	   > ${DATA}/${MODEL_FOLDER}/${MODEL_NAME}.log 2> ${DATA}/${MODEL_FOLDER}/${MODEL_NAME}.err &

