# Generic things
SOURCE=de
TARGET=en
LANGPAIR=${SOURCE}-${TARGET}

# Specific things to translation
#MODEL_NAME=de-en-shr-base_acc_43.15_ppl_63.81_e9.pt
#MODEL_NAME=/mnt/data/${LANGPAIR}-shr/base/de-en-md-shr_acc_79.67_ppl_2.58_e15.pt
#MODEL_NAME=/mnt/data/${LANGPAIR}-shr/base/en-de-jrc-shr_acc_77.19_ppl_3.02_e15.pt
MODEL_NAME=/mnt/data/${LANGPAIR}-shr-big/base/de-en-shr-big_acc_55.25_ppl_12.96_e15.pt

# Specify the source file
SRC_FILE=/mnt/data/${LANGPAIR}-md-shr/dev.bpe.sink.${SOURCE}

# Specify TP path
TP_PATH=/mnt/translation_pieces/${LANGPAIR}-md

#for lambda1 in 1.0
#do
#    for lambda2 in 1.1
#    do
 
#        if [ $(echo "${lambda1}==${lambda2}"|bc) -eq 1 ]
#        then
#            echo "Skipping ${lambda1} and ${lambda2}"
#            continue
#        fi
 
        lambda1=1.0
        lambda2=1.0
        extrald1=5.0
        extrald2=5.0

        echo "Lambda1: ${lambda1}"
        echo "Lambda2: ${lambda2}"
        echo "Extrald1: ${extrald1}"
        echo "Extrald2: ${extrald2}"
 
        # Call the OpenNMT-py script
        python3 translate.py \
                -model ${MODEL_NAME} \
                -src ${SRC_FILE} \
                -output ${SRC_FILE}.pred \
                -beam_size 5 \
                -min_length 2 \
                -use_guided \
                -tp_path ${TP_PATH}/dev_translation_pieces_md_20-th0pt0.pickle \
                -guided_n_max 4 \
                -guided_1_weight ${lambda1} \
                -guided_n_weight ${lambda2} \
                -guided_correct_ngrams \
                -guided_correct_1grams \
                -extend_with_tp \
                -extend_1_weight ${extrald1} \
                -extend_n_weight ${extrald2} \
                -replace_unk
        # CHANGE THE NAME OF THE FILE
    
        # Copy the predictions to the right folders
        HOME_PATH=/home/ubuntu/NMT-Code/attention_comparison/thesis/guided_nmt
        PRED_PATH=${HOME_PATH}/generate_results_de_en_da/preds
        MT_PATH=${HOME_PATH}/generate_results_de_en_da/mt_predictions

        v11=$(echo ${lambda1} | cut -f1 -d.)
        v12=$(echo ${lambda1} | cut -f2 -d.)
        v21=$(echo ${lambda2} | cut -f1 -d.)
        v22=$(echo ${lambda2} | cut -f2 -d.)
        
        v31=$(echo ${extrald1} | cut -f1 -d.)
        v32=$(echo ${extrald1} | cut -f2 -d.)
        v41=$(echo ${extrald2} | cut -f1 -d.)
        v42=$(echo ${extrald2} | cut -f2 -d.)
        
        POS=guided-20-0pt0-lambda-${v11}pt${v12}-${v21}pt${v22}-extrald-${v31}pt${v32}-${41}pt${42}
        #POS=base
        
        FN=$(echo ${SRC_FILE} | cut -d'/' -f5)
        cp ${SRC_FILE}.pred ${PRED_PATH}/${FN}.pred.${POS}
        sed -r 's/(@@ )|(@@ ?$)//g' ${PRED_PATH}/${FN}.pred.${POS} > \
                                    ${MT_PATH}/${FN}.pred.${POS}.merged

#    done
#done
